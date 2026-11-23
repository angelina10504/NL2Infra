# AGENTS.md — NL2Infra, final phase

You are the implementation agent for **NL2Infra**, a final-year DevOps course project
(BML Munjal University, Group-10: Angelina Gupta, Kavya Goswami, Sahil Dewani).

Read this whole file before doing anything. It is the standing context for every task.
Work arrives as phase files (`PHASE-0.md`, `PHASE-1.md`, …). **Do only the phase you are
given.** Never start the next phase on your own.

---

## 1. What NL2Infra is

A person describes infrastructure in plain English. An LLM turns it into Kubernetes YAML.
Deterministic checkers judge the YAML, and the LLM fixes what fails, until it passes or a
person takes over. The judge is never the author.

```
Request + role
  → 1 Guardrails      deterministic: topic scope, prompt injection, role/env permission, rate limit
  → 2 Plan            LLM call → JSON plan, validated by Pydantic
  → 3 Approve         human approves the plan before any YAML exists
  → 4 Generate        one LLM call per resource
  → 5 Gauntlet        deterministic checks (below)
  → 6 Fix             one LLM call per FAILING file only, max 5 rounds, then escalate to a person
  → 7 Pull request    real PR to the manifests repo (local folder fallback if no token)
  → 8 Deploy          NOT BUILT: ArgoCD configs exist in infra/argocd but have never been applied
```

**The Gauntlet (5).** Four checks always run; two more run when a project type is declared:

| Check | Question it answers |
|---|---|
| Plan conformance | Do the files still match the approved plan? |
| Checkov | Industry security rules |
| OPA / Conftest | Team policy, by role: `policies/base`, `staging`, `production` |
| `kubectl apply --dry-run=server` | Would a real cluster (kind) accept it? |
| Fit conformance (`fit.py`) | Does it contain what this project type must have? |
| Fit policy (`policies/fit`) | Is it the right shape for this project type? |

A crashed or silent checker is a **failure, never a pass**. Keep it that way.

**The LLM has no tools.** It only returns text. All actions are taken by our code.

Model: `openai/gpt-oss-120b` on Groq, temperature 0.1. **This comes only from the local
`.env` (`MODEL_NAME`).** `.env.example` says `llama3-70b-8192` and the code default in
`llm_service.py` is `llama-3.3-70b-versatile`. Anything that runs the app (a container, a
fresh checkout) must set `MODEL_NAME=openai/gpt-oss-120b` explicitly, or the benchmark is no
longer the same experiment.

---

## 2. Repo map

| Path | What it is |
|---|---|
| `contracts.py` | Pydantic contracts: Plan, Files, Violation, RunState, ProjectProfile, WorkloadType |
| `src/nl2infra/graph.py` | LangGraph pipeline: the nodes and edges above |
| `src/nl2infra/pipeline.py` | Drives the graph; streams node updates to the UI |
| `src/nl2infra/validators.py` | The Gauntlet |
| `src/nl2infra/llm_service.py` | Groq calls, retries, 429 back-off, token counting |
| `src/nl2infra/guardrails.py` | Input guardrails and rate limit |
| `src/nl2infra/fit.py`, `profiler.py` | Project-Aware Generation (8 workload types) |
| `src/nl2infra/gitops.py` | Pull request creation |
| `src/nl2infra/server.py` | FastAPI dashboard, `/metrics`, Slack interactions, SSE bell |
| `src/nl2infra/app.py` | Streamlit run/replay/compare-arms analysis view |
| `src/nl2infra/metrics.py` | Prometheus metrics (`prometheus_client`) |
| `src/nl2infra/timing.py` | Simple per-stage timing helper — the natural hook for tracing |
| `src/nl2infra/notifications/slack.py` | Slack approval flow with signature verification |
| `src/nl2infra/auth.py` | GitHub sign-in, sessions, project membership |
| `src/nl2infra/rag/` | Retrieval over policies and standards (arm D) |
| `policies/` | Rego packs + `checkov.yaml` (two Checkov rules skipped, with reasons) |
| `benchmarks/`, `eval/` | Benchmark runner, datasets, evaluation and charts |
| `observability/` | Prometheus, Alertmanager, Grafana configs and dashboard |
| `docker-compose.observability.yml` | Monitoring stack only — **the app has never been containerised** |
| `infra/kind-config.yaml`, `infra/argocd/` | Local cluster config; ArgoCD apps (never applied) |
| `tests/` | 23 test files (22 committed + untracked `test_research.py`); full suite must stay green |
| `.github/workflows/ci.yml` | CI workflow — exists locally, but `.github/workflows/` is gitignored |

Run locally today: `make run` (uvicorn on :8000), `make streamlit`, `make test`.

---

## 3. Current status

| Area | State |
|---|---|
| Pipeline, Gauntlet, fix loop, guardrails | Built and tested |
| Benchmark arms A/B/C | **Run** — 20 requests (easy + medium), 1 repeat |
| Arm D (RAG), fit benchmark, hard tier | Code exists, **not run** |
| Pull requests, Slack approval, team access | Built |
| Prometheus / Grafana / Alertmanager | Built, runs under Docker, app runs on the host |
| ArgoCD deploy | Configs only, never applied |
| **Podman** | **Not started** |
| **OpenTelemetry** | **Not started** |

**Recorded benchmark result — do not disturb it.** Final pass rate A 0/20, B 2/20, C 19/20;
all-checks-pass 0 / 0 / 19. Anything you change must leave the benchmark reproducible.

**Local and GitHub are both at `148d23e`** (29 October), plus uncommitted work: a `/research`
page and `/api/research` endpoint, a matching ngrok policy rule, and a Prometheus scrape-port
change to 8001. Phase 0 settles these.

**No kind cluster exists right now.** It must be recreated (`make cluster`, `make namespaces`)
before the dry-run checker or the real Gauntlet can work.

---

## 4. Goal of this final phase

Two things, in this order:

### A. Run the whole system on Podman
- Containerise the app (`Containerfile`), with Checkov, Conftest, OPA and kubectl inside.
- One compose file for everything: app, Prometheus, Alertmanager, Grafana, OTel Collector,
  Jaeger. Started with `podman compose up -d`.
- Target: kind also runs on Podman (`KIND_EXPERIMENTAL_PROVIDER=podman`), so Docker Desktop is
  no longer needed. This is attempted as a spike first, with a documented fallback.

### B. OpenTelemetry for monitoring, especially latency
- One trace per request. One span per pipeline stage, per Gauntlet checker, per LLM call,
  per fix round, per rate-limit wait.
- Spans carry attributes: request id, role, namespace, workload type, arm, round number,
  model, tokens in/out, violation count, rule ids, outcome.
- OTel Collector receives OTLP, sends traces to Jaeger, and derives per-stage latency metrics
  (span metrics) into Prometheus so Grafana can chart them.
- Grafana gets a Jaeger datasource and latency panels, so monitoring is one screen.

**Why:** examiner feedback asked for Podman, live monitoring, and "work on the latency part".
A trace waterfall shows exactly where a request's time goes: model calls vs checkers vs
rate-limit waits.

---

## 5. Hard rules — never break these

1. **Do not change what is measured.** No edits to Gauntlet logic, policies, prompts, the
   benchmark runner, evaluation, or datasets unless a phase file says so explicitly.
   Instrumentation wraps behaviour; it does not alter it.
2. **Telemetry must never fail or block a run.** If the collector is down or unset,
   tracing is a no-op. Add a test proving it.
3. **Containers follow our own policies.** Run as non-root with a fixed high UID
   (≥ 10000), set CPU and memory limits, no privileged mode, and **pin every image to an
   exact version — no `:latest`** (the current monitoring compose uses `:latest`; fix it).
   Our policy rejects these in generated YAML, so our own infrastructure must pass them too.
4. **No secrets anywhere they can leak.** Never bake `.env` or keys into an image, a
   committed file, a log line, a span attribute or your output. Secrets are injected at run
   time. Never print a key value, not even partially.
5. **Ask before:** installing anything on the host, making real LLM calls, committing,
   pushing, or deleting files. Real LLM calls burn shared quota — each free Groq account
   gives roughly 42–57 benchmark runs per day. Use `MOCK_MODE=true` whenever you can.
6. **Move, don't delete.** If something must go, move it aside and say where.
7. **The full test suite must pass at the end of every phase.** Report exact counts.
8. **Verify, don't assume.** Run it and show the output. If you only reasoned about
   something, say "not verified".
9. **When blocked, stop and report.** Do not work around a failure with a hack, a disabled
   check or a skipped test.

---

## 6. Known pitfalls

- **macOS Podman runs inside a Linux VM** (`podman machine`). kind on Podman needs that VM in
  rootful mode. Don't run Docker Desktop and the Podman VM together on this MacBook Air.
- **The app container must reach kind's API server.** Inside a container, `localhost` is the
  container. The expected fix is to join the app container to kind's `kind` network and use
  `kind get kubeconfig --internal`. Never mount a kubeconfig that points at `127.0.0.1`.
- **`host.docker.internal` is Docker-specific.** Once the app is inside the compose network,
  Prometheus should scrape it by service name (`app:8000`), not via the host.
- **The app port is 8000.** It is set in the Makefile, README, `.env.example`, tests, and the
  Alertmanager webhook. The uncommitted Prometheus change to 8001 is being reverted in Phase 0.
  Use 8000 everywhere, inside and outside containers.
- **Slack needs a public URL** (ngrok — see `docs/temporary-sharing.md` and
  `infra/ngrok-team-policy.json`), and that URL changes on every restart.
- **The laptop sleeping kills Docker/Podman VMs.** Long runs go under `caffeinate -i`.

---

## 7. How each phase is reported

End every phase with exactly this, and nothing claimed that you did not check:

```
## Phase N report
Changed:       <files, one line each, with why>
Commands run:  <commands, with trimmed output>
Tests:         <X passed, Y failed, Z skipped>  (Rego: A of B)
Verified:      <what you actually ran and saw>
Not verified:  <what you reasoned about but did not run>
Open issues:   <problems, risks, anything that contradicts this file>
Needs a decision: <questions for the team, or "none">
```

Then stop and wait for the next phase file.
