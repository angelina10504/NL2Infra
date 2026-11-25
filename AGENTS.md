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

Model: `openai/gpt-oss-120b` on Groq, temperature 0.1. It is set in `.env` and `.env.example`
(`MODEL_NAME`). The code default in `llm_service.py` is still `llama-3.3-70b-versatile`, so
anything that runs the app (a container, a fresh checkout) must set `MODEL_NAME` explicitly.
All 60 recorded benchmark runs used `openai/gpt-oss-120b`.

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
| `compose.yaml` | The whole stack on Podman: app, Prometheus, Alertmanager, Grafana (Phase 3) |
| `infra/legacy/docker-compose.observability.yml` | Old Docker-only monitoring stack, kept for reference |
| `secrets/` (gitignored) | `alert_webhook_token`, `grafana_admin_password` — file secrets, never printed |
| `Containerfile`, `.containerignore` | App image: non-root UID 10001, baseline tool versions (Phase 2) |
| `docs/podman.md`, `docs/container.md`, `docs/compose.md` | kind on Podman; the image; the compose stack |
| `infra/kind-config.yaml`, `infra/argocd/` | Local cluster config; ArgoCD apps (never applied) |
| `tests/` | 25 test files, 437 tests; full suite must stay green |
| `.github/workflows/ci.yml` | CI workflow — exists locally, but `.github/workflows/` is gitignored |

Run on the host: `make run` (uvicorn on :8000), `make streamlit`, `make test`.
Run in a container: `make image PROVIDER=podman`, `make image-test PROVIDER=podman`,
`make image-run PROVIDER=podman`.
Whole stack: `make up` / `make down` / `make ps` / `make logs` / `make config` (hides `.env` values).

---

## 3. Current status

| Area | State |
|---|---|
| Pipeline, Gauntlet, fix loop, guardrails | Built and tested |
| Benchmark arms A/B/C | **Run** — 20 requests (easy + medium), 1 repeat |
| Arm D (RAG), fit benchmark, hard tier | Code exists, **not run** |
| Pull requests, Slack approval, team access | Built |
| Prometheus / Grafana / Alertmanager | Run in compose on Podman; alert path verified end to end (webhook uses a bearer token) |
| ArgoCD deploy | Configs only, never applied |
| **Podman** | Done: kind on Podman, app image, and the full compose stack (`make up`), all hardened |
| **OpenTelemetry** | **Host and image suites passed (442 tests each). Verified live OTLP export, Jaeger queries through Grafana, and four checker latency series in Prometheus. Authenticated mock UI run verified through Grafana: planning and generation shared the request ID, generation linked to planning, and the stage panel returned nine series. REQUIRED for the final evaluation — never defer it to "future work"** |

**Recorded benchmark result — do not disturb it.** Final pass rate A 0/20, B 2/20, C 19/20;
all-checks-pass 0 / 0 / 19. Anything you change must leave the benchmark reproducible.

**Baseline (Phase 3, commit `f33d7dd`):** 437 tests pass on the host and inside the container
(36 against the real cluster), Rego 46/46. Python 3.12.13, Checkov 3.3.26, Conftest 0.69.0
with embedded OPA 1.19.0 (Homebrew's 0.69.0 reports `dev`; same release), kubectl/cluster
v1.37.0, kind v0.33.0. These match all 60 recorded benchmark runs and must stay the same.
Exact Python pins are in `requirements.lock`.

**Runtime now:** Podman 6.2.0, rootful machine `podman-machine-default` (4 CPU, 4096 MB,
libkrun). kind cluster `nl2infra` (node image pinned by digest) runs on Podman. `make up` runs
the stack: app 1 GiB/1 CPU, Prometheus v3.16.0, Alertmanager v0.35.0, Grafana 13.3.1 (each
pinned by digest, non-root, caps dropped, read-only root, ports on 127.0.0.1). Idle memory with
kind ≈ 1.1 GB of 4 GB. Docker Desktop is quit; its data (~8.6 GB, includes other projects'
volumes) is kept. `podman compose` uses the external `docker-compose` v5.5.0.

**Containers keep their own run history.** Inside the container `NL2INFRA_RUNS_DIR` points at a
named volume; the host's `runs/benchmark` and `runs/pilot` are mounted read-only and are never
written by a container. Container run logs get the commit from `NL2INFRA_GIT_COMMIT`.

**The laptop has 8 GB RAM and about 6 GB free disk.** This is the main constraint.

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

**Both A and B are required deliverables for the final evaluation.** Neither may be moved to
"future work", dropped, or reduced to docs only. If something blocks them, stop and report the
blocker with options that still deliver them.

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
  Alertmanager webhook.
  Use 8000 everywhere, inside and outside containers.
- **Slack needs a public URL** (ngrok — see `docs/temporary-sharing.md` and
  `infra/ngrok-team-policy.json`), and that URL changes on every restart.
- **Finder creates `.DS_Store` files** in any folder it opens. A test that reads every file in
  `policies/` breaks on them.
- **Use `podman`, not `docker`.** The docker CLI's context is still `desktop-linux`, so `docker`
  commands fail while Docker Desktop is quit.
- **Conftest reports its version as `dev`.** Any other Conftest install must embed OPA 1.19.0
  and pass the same 46 Rego tests.
- **The Podman VM disk only grows.** After building or pulling images, run
  `podman machine ssh -- sudo fstrim -av` to give free space back to macOS.
- **Build with `--format docker`.** Podman's default OCI format drops `HEALTHCHECK`.
- **kind picks Docker when the docker CLI exists.** Always pass `PROVIDER=podman`.
- **`env_file: .env` exposes the app's keys** to `podman compose config` and `podman inspect`.
  Use `make config`. Never print either output whole.
- **Most app routes need a GitHub sign-in.** Don't work around auth to test something; ask the
  team to sign in in a browser.
- **Check `MOCK_MODE` inside the container before any run.** `.env` overrides the image default.
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
