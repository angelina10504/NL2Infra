# Decisions log

Short record of non-obvious choices made during the industry-grade upgrade. Two to three
lines each, newest first.

## Setup / baseline
- **Test runner interpreter.** Dependencies (langgraph, streamlit, checkov, …) are installed in
  the Windows `Python 3.13` (`C:\Program Files\Python313`) interpreter, not Anaconda. Tests are run
  with that interpreter. Installed `pytest` into it to get a baseline.
- **Cross-platform `fake_tool` test helper.** `tests/test_validators.py` wrote a `#!/bin/sh` script
  as a fake scanner; Windows `subprocess` ignores the shebang so three tests failed only on Windows.
  Made `fake_tool` emit a `.bat` wrapper on `os.name == "nt"`. No production code changed.
- **Baseline:** 185 passed, 29 skipped (the 29 are `real_tools` tests needing checkov/conftest/kubectl
  binaries on PATH).

## Phase 1 — Guardrails
- **Roles kept backward-compatible.** The teacher asked for `viewer`/`developer`/`admin`. The existing
  code keys OPA policy strictness off `junior_dev`/`senior_dev`/`platform_admin` (`ROLE_PACKS`). Rather
  than rename and break the Gauntlet, rules and UI, the new names are canonical and the legacy names are
  aliases (`junior_dev`,`senior_dev`→`developer`; `platform_admin`→`admin`). `ROLE_PACKS` extended to
  accept both.
- **`check_guardrails(state) -> str` stays stateless.** The benchmark and eval call it to build a
  dry-run "would be refused" list, so it must not have rate-limit side effects. Rate limiting lives in a
  separate `RateLimiter` wired into the graph's guardrails node only.
- **Environment detected from the prompt text** (deterministic, pre-LLM) so a blocked request still
  spends zero tokens — matching the project's "blocked before inference" guarantee.

## Phase 2 — Events & in-dashboard notifications
- **Store = JSONL, not SQLite.** Local, zero-dependency, trivially inspectable. Path is
  `NL2INFRA_EVENTS_FILE` (default `events.jsonl`); tests redirect it per-test via an autouse fixture.
- **`approval_required` emitted from the pipeline layer, not the approve node.** LangGraph re-runs a
  node's body on resume, so code before `interrupt()` fires twice. The pipeline emits it exactly once,
  when `_drive` sees the run pause. The other events (`request_blocked`, `fix_loop_exhausted`,
  `pr_opened`) are emitted from their graph nodes, which run once. All emits use `emit_event_safe`.
- **SSE generator lives in `notifications.event_stream` (not the route) and does NOT call
  `request.is_disconnected()`.** That call consumes the ASGI receive channel and then blocks forever,
  starving the queue poll. Disconnect is handled by cancellation into `finally`. Making it a plain
  async generator also lets tests drive it directly (httpx ASGITransport buffers infinite streams).
- **Notifications UI lives in the FastAPI HTML dashboard** (`templates/index.html`), the natural home
  for a live bell + toasts via `EventSource`. The Streamlit app stays the run/replay analysis view.
- **`channels` is a dispatch table** (`CHANNELS`, default `("dashboard",)`). Email/Slack = register one
  function with `register_channel(...)`; no other code changes. Not implemented now, by design.

## Phase 3 — Observability (Prometheus + Grafana + Alertmanager)
- **Metrics recorded from the pipeline, not scattered.** `metrics.py` holds the Prometheus singletons;
  `pipeline._drive` records one run at completion (status, env, rounds, duration, tokens, latency) and
  maintains the `pending_approvals` gauge (inc on pause, dec on completion). Per-round violations are
  counted in the graph's gauntlet node. All metric calls are wrapped so they never break a run.
- **Counter names keep the `_total` suffix.** prometheus_client leaves a name that already ends in
  `_total` unchanged, so `Counter("nl2infra_requests_total")` exposes exactly `nl2infra_requests_total`
  (matching the spec), not `..._total_total`.
- **Prometheus scrapes the host, not a container.** The app runs on the host (`:8000`); the stack uses
  `host.docker.internal:8000` (with `extra_hosts: host-gateway` for Linux parity). Alertmanager posts to
  `host.docker.internal:8000/alerts/webhook`.
- **Alertmanager → `/alerts/webhook` → `emit_event`** so system-health alerts also land in the dashboard
  bell. `FixLoopExhaustedSpike` uses `status="escalated"` as the signal (there is no separate exhausted
  counter); documented as a proxy.
- **Docker not installed on the dev box**, so the stack is validated by parsing every config file and
  checking the Grafana dashboard only queries exported metrics (`test_observability_config.py`). Running
  it needs `docker compose -f docker-compose.observability.yml up -d` on a machine with Docker.

## Phase 4 — RAG over policies
- **Offline TF-IDF embeddings by default, not a downloaded model.** scikit-learn TF-IDF + FAISS
  (inner-product on L2-normalised vectors) keeps the KB free, local and CI-safe with no network.
  `NL2INFRA_EMBEDDINGS=hf` opts into sentence-transformers. langchain is a listed dep but the retriever
  is deliberately light. (chromadb/llama_index aren't installed; FAISS is, so FAISS is the vector store.)
- **A camelCase/identifier-splitting preprocessor** (`runAsNonRoot` → `run as non root`) plus a short
  keyword hint per OPA rule makes plain-English queries ("running as root", "latest tag") retrieve the
  right policy. Verified by a parametrized test over 8 queries.
- **RAG is an optional `retriever` on `LLMService`.** When set, the generate prompt gets a grounding
  block (top policies + one approved example). Benchmark arm D is just arm C with that retriever wired in.
- **`--arm all` stays A,B,C; arm D is opt-in (`--arm D`).** Keeps every existing benchmark/eval test and
  the 3-column evaluate.py unchanged. Arm D reuses the shared plan file, so `--arm all` then `--arm D`
  score against the same plans.
- **The "why?" bot is retrieval-only (no LLM).** It indexes run-log summaries and composes a deterministic,
  cited explanation — free, offline, and it cannot hallucinate a reason. Exposed at `POST /api/why`.

## Phase 5 — Benchmark dataset & evaluation
- **Dataset expanded to 30 correctness + 40 adversarial**, same JSONL format. Correctness prompts are
  role/environment-consistent so the guardrails never falsely refuse them (verified: 0/30). Adversarial
  entries carry a `category` covering all eight required attack families.
- **Honest guardrail numbers, computed deterministically:** 45% (18/40) adversarial refusal, 0% false
  refusal. The 22 "let through" are manifest-level attacks (privileged, hostPath, latest tag, missing
  limits, wrong apiVersion, plaintext secret) — by design these are the Gauntlet's job, not the input
  filter's. This is the correct figure for a two-layer design and is documented as such in results/.
- **The full 70×3×4-arm LLM benchmark was NOT run here** (no GROQ_API_KEY, no checkov/conftest/kubectl).
  `results/README.md` gives the exact commands and is explicit about not fabricating numbers. The static
  "100% / 40 of 40" tiles in the HTML dashboard are flagged as illustrative placeholders.
- **evaluate.py / charts.py both learned arm D.** Charts use the Agg backend and are tested against
  synthetic logs (no real run needed to prove they render).

## Phase 6 — GitHub Actions CI
- **One `test` job gates every PR:** ruff (errors only — `E9,F63,F7,F82` — so existing style never
  fails the build), full pytest, `conftest verify --policy policies` (all 33 Rego tests run together,
  as they share base fixtures), Checkov on `samples/manifests` (soft-fail — it is a report gate; the
  Gauntlet is the real enforcer), and an offline `run_benchmark.py --dry-count` smoke test.
- **The AI reviewer is advisory and comment-only.** A separate `ai-review` job (PR-only,
  `pull-requests: write`, `contents: read`) runs `.github/scripts/ai_review.py`, which reuses the
  project's single LLM entry point (`LLMService._call`) to summarise diff risks and posts one comment
  via `gh`. It never approves or merges, fails soft (exits 0 on any error / missing key), and uses
  `continue-on-error` so it can never block a PR.
- **CI badge + full README added** (`angelina10504/NL2Infra`). The README also folds in most of
  Phase 7's doc asks (Mermaid architecture, quick start, tests, observability, ethics & limits,
  tier-2 sketch).
