# NL2Infra Architecture & Build Guide

Sep 28, 2026 · @Tushar

## Summary

For the project, NL2Infra needs **one machine, zero rented servers, no cloud storage and no paid services**. Everything runs on one laptop or one Linux VM with 16 GB of RAM. The only external dependencies are a free LLM API and a free GitHub account.

This doc covers two things: the exact setup the team builds for the project, and the production design we would use in a real company. The second exists so we can answer production questions in the viva with a concrete design, not guesses.

| Question | For the project | In production |
| --- | --- | --- |
| Servers needed | 0 rented. 1 machine runs everything | 3-node management cluster + separate target clusters |
| Cloud storage | Not needed. Local files + Git | Object storage (S3) for run artifacts |
| Database | JSON files per run (SQLite optional) | Managed PostgreSQL |
| AI model | Free hosted API (Groq or Gemini) | Paid API with a fallback provider, or a self-hosted model |
| Kubernetes | kind, inside Docker | Managed Kubernetes (EKS, AKS or GKE) |
| Cost | Rs 0 | Mainly LLM tokens + cluster nodes |
| Deploy approval | A person merges the PR | Same, plus branch protection and required reviewers |

Two decisions drive everything else:

1. **The AI never touches the real cluster.** It writes files. Rule engines check them. A person merges. ArgoCD deploys.
2. **All components talk through four fixed data formats** (`contracts.py`). This lets three people build in parallel.

## Two deployment tiers

We build Tier 1. We design and document Tier 2. The code is the same in both. Only where it runs, and what backs its storage, changes.

|  | Tier 1: project build | Tier 2: production reference |
| --- | --- | --- |
| Purpose | Demo, benchmark, report | How a real platform team would run it |
| Where it runs | One laptop or one Ubuntu VM | Management Kubernetes cluster |
| Users | Our team, roles simulated with a dropdown | Many developers, real login (SSO) |
| Request handling | Streamlit calls the graph directly | API + job queue + worker pods |
| Run logs | `runs/*.json` on disk | PostgreSQL + object storage |
| Secrets | `.env` file, never committed | Secrets manager (Vault or AWS Secrets Manager) |
| Target cluster | Same kind cluster | Separate clusters per environment (dev, staging, prod) |
| Deploy | ArgoCD in kind | ArgoCD per cluster or one hub ArgoCD |

**Rule for the team:** write every storage call behind a small function (`save_run()`, `load_runs()`). In Tier 1 it writes a JSON file. In Tier 2 the same function writes to Postgres. Nothing else in the code changes. This is the answer when someone asks "how would this scale?"

## System architecture

&#91;embedded content: Tier 1 architecture · one app process, two internet services, one local cluster\]

The app is one Python process with seven parts. The orchestrator calls four workers on the right. Dashed boxes are internet services. The validators only send dry-run requests, which never create anything. The only thing that changes the cluster is ArgoCD, after a person merges a pull request.

| Component | Job | Owner |
| --- | --- | --- |
| Streamlit UI | Takes the request and role, shows the plan for approval, streams progress | P1 |
| Guardrails | Refuses off-topic, injection, wrong-role and over-limit requests | P1 + P2 |
| LangGraph orchestrator | Runs the stages in order, carries `RunState`, enforces the 5-round cap | P1 |
| LLM service | The only code that calls the model: `plan()`, `generate()`, `fix()` | P2 |
| Gauntlet validators | Runs Checkov, Conftest and dry-run; returns normalised `Violation` objects | P3 |
| GitOps module | Commits clean files to a branch and opens the pull request | P3 |
| Run log store | Writes one JSON file per run with every draft, violation and timing | P1 |

## Servers and hardware

**Tier 1 needs one machine and no rented servers.** The AI model runs on the provider's servers, so nothing heavy runs locally. Memory is the only real constraint. The figures below are working estimates; measure with `docker stats` in week 1 and update them.

| What runs | Approx. memory | Needed when |
| --- | --- | --- |
| Operating system + browser | 4 GB | Always |
| Docker + kind node (Kubernetes control plane) | 1.5 to 2 GB | Always |
| ArgoCD (runs as pods inside kind) | about 1 GB | Only for the deploy demo |
| NL2Infra app + Checkov scans | about 1 GB | During runs |
| **Total** | **8 to 9 GB** |  |

| Spec | Minimum | Recommended |
| --- | --- | --- |
| RAM | 8 GB (stop ArgoCD during benchmarks) | 16 GB |
| CPU | 4 cores | 6 to 8 cores |
| Free disk | 30 GB | 50 GB |
| GPU | Not needed | Not needed |
| Internet | Needed for the LLM API and GitHub | Stable connection for the demo |

**If you use a VM,** give the VM at least 8 GB RAM and 4 vCPUs, which means the host laptop needs 16 GB.

**Tier 2 (production reference) sizing.** A run spends most of its 30 to 90 seconds waiting on the LLM, so load is small. Scale the worker count, not the machine size.

| Component | Count | Size (starting point) |
| --- | --- | --- |
| Management cluster nodes | 3 (survives one node failing) | 4 vCPU, 16 GB each |
| API pods | 2 | 0.5 vCPU, 512 MB |
| Worker pods (run the graph + scanners) | 2 to 6, autoscaled on queue length | 1 vCPU, 1 GB |
| Redis (job queue, rate-limit counters) | 1 | 1 GB |
| PostgreSQL | 1 managed instance, standby replica | 2 vCPU, 8 GB |
| Object storage | 1 bucket | pay per GB |
| Target clusters | Already exist (dev, staging, prod) | not part of NL2Infra |

## Storage

**We do not need cloud storage for the project.** Every piece of data is either small enough for local files or belongs in Git. AWS S3 is only part of the production answer.

| Data | Size per run | Tier 1: where it lives | Tier 2: where it lives | Why there |
| --- | --- | --- | --- | --- |
| Final manifests (YAML) | a few KB | GitHub repo `nl2infra-manifests` | Same | Git is the record of what runs; ArgoCD reads only from Git |
| Run log: prompt, plan, metrics, status | 5 to 50 KB | `runs/<request_id>.json` | PostgreSQL table `runs` | Queried for the evaluation charts |
| Violations per round | small, many rows | inside the run JSON | PostgreSQL table `violations` | Grouped by rule, tool and model |
| Every draft + raw scanner output | 20 to 200 KB | `runs/<request_id>/iter_<n>/` folder | Object storage (S3), key = request id | Large, rarely read, needed for audits and the demo diff |
| Policy pack (Rego rules) | a few KB | `policies/` in the app repo | Same, versioned | Rules change through pull requests like code |
| API keys, GitHub token | tiny | `.env` (gitignored) | Secrets manager | Never in Git, never in logs |
| Cluster state | n/a | inside kind | Managed control plane | Kubernetes owns it |

Two repositories, not one:

1. **`nl2infra`**: the application code, policies, tests. Humans and CI write here.
2. **`nl2infra-manifests`**: only generated YAML, one folder per environment. The agent writes branches here, humans merge, ArgoCD reads. Keeping it separate means the agent's GitHub token can never modify the app's own code.

30 benchmark runs + 40 adversarial runs produce well under 50 MB in total. A laptop disk is enough.

## Security model

**The LLM has no tools.** It only returns text. It cannot run kubectl, call GitHub or read files. Every action is taken by our own code, after deterministic checks. This is the strongest single defence against prompt injection: even a fully hijacked model can only produce YAML that still has to pass the Gauntlet and a human review.

| Asset | Risk | Control in Tier 1 | Control in Tier 2 |
| --- | --- | --- | --- |
| LLM API key | Leaked key used by others | `.env`, gitignored, redacted from logs | Secrets manager, rotated |
| GitHub token | Agent pushes to the wrong place | Fine-grained token for `nl2infra-manifests` only: contents + pull requests, no admin | GitHub App with the same narrow scope |
| `main` branch of manifests repo | Unreviewed change reaches the cluster | Branch protection: PR required, 1 approval | Same + the Gauntlet re-runs in GitHub Actions as a required check |
| Cluster | Validator creates real objects | Code always passes `--dry-run=server`; kind is disposable | Dry-run against a separate sandbox cluster, never production |
| Cluster | ArgoCD deploys something dangerous | ArgoCD project limited to `dev` and `staging` namespaces | Per-project allow lists: namespaces and resource kinds (no ClusterRole, no privileged pods) |
| Secrets in generated YAML | Real passwords typed into files | OPA rule rejects any literal value in a password-like env var; Secret manifests use placeholders | External Secrets Operator pulls real values from the secrets manager |
| Data sent to the model | Company details leave the network | Only the request and generated YAML are sent; never keys | Paid API with a no-training agreement, or a self-hosted model |

**Why the Gauntlet also runs in CI:** our app could have a bug that skips a check. Running the same Checkov and OPA rules as a required GitHub Actions check on the manifests repo means an unsafe file cannot be merged even if the app is bypassed. Two independent gates, same rules.

**Server-side dry-run caveat:** Kubernetes permissions cannot tell a dry-run from a real create. The account that dry-runs could create objects if the code were changed. That is why production validates against a sandbox cluster.

## Reliability and failure handling

**Every failure ends a run in a named state; nothing hangs and nothing half-deploys.** Build each row below into the code and test it with `MOCK_MODE` before the real model is connected.

| Failure | Detection | Handling | Final status |
| --- | --- | --- | --- |
| LLM returns prose instead of JSON | Pydantic validation fails | Strip code fences, parse again; on failure retry once with the error in the prompt | `escalated` after 2 bad plans |
| LLM API timeout or 5xx | 30 s timeout, HTTP error | Retry 3 times with backoff (2 s, 4 s, 8 s) | `escalated` with reason `llm_unavailable` |
| Free-tier rate limit (HTTP 429) | Status code | Wait for the `Retry-After` time; in Tier 2 switch to the fallback provider | Continues, delay logged |
| Fix loop never converges | `iterations >= 5` | Stop, save every draft | `escalated` |
| Same rule fails twice in a row | History check | Send the failed attempt back with "this did not work" | Continues |
| Checkov or Conftest crashes | Non-zero exit, no JSON | Record as a `tool_error`, never as a pass | `escalated` |
| Cluster unreachable for dry-run | Connection error | Skip is not allowed; retry once | `escalated` |
| GitHub push fails | API error | Keep files in the run folder, retry the PR step alone | `passed` with `pr_url` empty, retryable |
| App crashes mid-run | Tier 2: job not acknowledged | Tier 1: rerun by hand. Tier 2: queue redelivers to another worker | Run resumes from its last saved state |

**Rule: a checker that fails to run is a failure, not a pass.** A crashed scanner returning an empty list would look exactly like clean files. The code must tell those apart.

**Idempotency:** each run has a `request_id`, and the pull request branch is named `nl2infra/<request_id>`. Retrying a run updates the same branch instead of opening a duplicate PR.

## Observability and evaluation data

**The run log is both our monitoring and our results chapter.** Every run writes one JSON file from the first working day. A script in `eval/` loads all files into pandas and produces every chart in the report.

Each run log records:

- `request_id`, `user_role`, `user_prompt`, `model`, `status`, `rejection_reason`, `pr_url`
- The plan, every draft and every violation, per iteration
- `metrics`: planning, generation, per-tool validation and fix times; total runtime; tokens in, tokens out, number of calls

| Question the panel asks | Metric that answers it | Source |
| --- | --- | --- |
| How often is raw LLM output unsafe? | First-draft pass rate | Violations at iteration 1 |
| Does the loop work? | Average fix rounds; final pass rate | `iterations`, `status` |
| Which checker earns its place? | Violations caught per tool | `violations[].tool` |
| How long does it take? | Median and p95 runtime, split into model time and pipeline time | `metrics` |
| What does a run cost? | Tokens per run × provider price | `metrics.tokens` |
| Are guardrails safe and not over-strict? | Refusal rate on adversarial; false refusals on legitimate | `status = rejected` by benchmark set |
| Which model is better? | All of the above, grouped by `model` | `model` |

**Report model time separately.** Free-tier API latency changes with the provider's load. Mixing it into one number makes our pipeline look slow for reasons outside our system.

**Tier 2 addition:** the same numbers exported as Prometheus metrics (runs by status, fix rounds, violations by rule, LLM latency) with a Grafana dashboard and an alert when the escalation rate rises.

## Repository structure and data contracts

**Build this layout on day one, then fill it.** It extends the scaffold already shared. Each folder has one owner so pull requests rarely collide.

```
nl2infra/                         app repository
├── contracts.py                  4 shared data formats            everyone, change only by team agreement
├── src/nl2infra/
│   ├── graph.py                  LangGraph nodes + routing        P1
│   ├── app.py                    Streamlit UI                     P1
│   ├── storage.py                save_run(), load_runs()          P1
│   ├── timing.py                 timer helper that fills metrics  P1
│   ├── mocks.py                  fake nodes + MOCK_MODE           P1
│   ├── guardrails.py             scope, injection, role, rate     P1 + P2
│   ├── llm_service.py            plan(), generate(), fix()        P2
│   ├── prompts/                  one text file per prompt         P2
│   ├── validators.py             checkov, opa, dry-run, normalise P3
│   └── gitops.py                 branch, commit, open PR          P3
├── policies/
│   ├── base/                     rules for every role             P3
│   ├── staging/                  added for senior_dev             P3
│   └── production/               added for platform_admin         P3
├── benchmarks/
│   ├── correctness.jsonl         30 legitimate requests            P2
│   └── adversarial.jsonl         40 attack requests                P2
├── eval/                         load runs, make charts            P1
├── infra/
│   ├── kind-config.yaml          cluster definition                P3
│   └── argocd/                   ArgoCD project + application      P1
├── fixtures/                     sample plan, files, violations    everyone
├── tests/                        contract + unit tests             everyone
├── runs/                         run logs (gitignored)
├── .github/workflows/ci.yml      tests + lint on every PR          P1
├── Makefile                      make setup / test / cluster / run
├── requirements.txt
└── .env.example

nl2infra-manifests/               generated YAML only, separate repo
├── dev/
├── staging/
└── .github/workflows/gauntlet.yml   re-runs Checkov + OPA as a required check
```

**The four contracts** (already in `contracts.py`):

| Contract | Produced by | Consumed by | Shape |
| --- | --- | --- | --- |
| `Plan` | `plan()` | approval UI, `generate()` | list of `Resource`: type, name, namespace, `spec` dict |
| `Files` | `generate()`, `fix()` | validators, gitops | `dict[filename, yaml_text]` |
| `Violation` | validators | `fix()`, run log | tool, rule\_id, severity, file, line, message, resource |
| `RunState` | graph | every node, run log | request, role, plan, files, violations, iterations, history, metrics, model, status |

Changing a contract = a pull request all three people approve, plus updated fixtures in the same PR.

## Environment setup

**Avoid a Windows VM.** Docker on Windows needs WSL2, which is itself a virtual machine. Inside a Windows VM that means a VM inside a VM (nested virtualisation), which often fails or runs slowly, especially on VirtualBox. Containers on Linux need no nesting at all.

| Option | How it works | Nested virtualisation | Verdict |
| --- | --- | --- | --- |
| A. Windows laptop, no VM | Docker Desktop + WSL2 Ubuntu; code lives inside Ubuntu | No | **Recommended** |
| B. Ubuntu VM (VirtualBox or VMware) | Docker Engine installed directly in Ubuntu | No | Good if a VM is required |
| C. Windows VM | Docker Desktop inside the VM needs WSL2 inside the VM | Yes | Avoid |

If the course requires a VM, choose **Option B with Ubuntu 24.04 LTS**, 8 GB RAM, 4 vCPUs, 50 GB disk.

**Setup steps (inside Ubuntu, for option A or B):**

1. Docker. Option A: install Docker Desktop on Windows and enable WSL2 integration for Ubuntu. Option B: `sudo apt install -y docker.io` then `sudo usermod -aG docker $USER` and log out and in.
2. Check: `docker run hello-world`.
3. kind and kubectl: install from the official pages (kind.sigs.k8s.io quick start, kubernetes.io kubectl install for Linux). Use the current releases.
4. Cluster: `kind create cluster --name nl2infra --config infra/kind-config.yaml`, then `kubectl get nodes` until it shows `Ready`.
5. Python tooling: install uv (docs.astral.sh/uv), then `uv venv --python 3.12` and `uv pip install -r requirements.txt checkov`.
6. Conftest: download the Linux binary from its GitHub releases page.
7. ArgoCD: `kubectl create namespace argocd`, then apply the stable install manifest from the ArgoCD getting-started guide.
8. Check everything: `checkov --version`, `conftest --version`, `pytest -q` (contract tests pass).

**Option A memory limit:** WSL2 takes up to half of RAM by default. Set it in `C:\Users\<you>\.wslconfig` with `[wsl2]` and `memory=8GB`, then run `wsl --shutdown`.

**Every teammate runs steps 1 to 8 on their own machine in week 1.** The demo runs on the strongest machine, but everyone needs a local copy for development.

## CI for the repositories

**A DevOps project must use DevOps on itself.** Both repositories get a GitHub Actions workflow in week 1. Both fit inside GitHub's free minutes.

| Repo | Trigger | Steps | Blocks merge if |
| --- | --- | --- | --- |
| `nl2infra` | Every push and PR | Install deps, lint with ruff, run pytest (contract tests + unit tests with `MOCK_MODE`), run `conftest verify` on the policy tests | Any step fails |
| `nl2infra-manifests` | Every PR | Run Checkov and Conftest with the same policy pack on the changed files | Any violation |

Rules for working together:

- `main` is protected in both repos: pull request required, CI must pass, one teammate approves.
- One branch per task, named `p1/graph-routing`, `p3/checkov-normaliser` and so on.
- No real LLM calls in CI. Tests use mocks and fixtures, so CI costs no API quota and never needs a key.
- Every Rego rule gets a test file in `policies/` with one passing and one failing example.

## Team ownership and build order

&#91;embedded content: Build order · 8 weeks, 3 lanes, 4 gates\]

Each lane works on mocks and fixtures until a gate, so nobody waits on anyone. The week-2 gate matters most: one request must run end to end, even if half the parts are still mocked.

| Gate | Done when |
| --- | --- |
| Contracts frozen (end of week 1) | `contracts.py` and fixtures merged; all three machines pass `pytest`; P3 has handed P2 ten real Checkov outputs |
| Skeleton end to end (end of week 2) | One request goes from the UI to a run log file, with real Checkov and at least one real LLM call |
| Loop works on real requests (end of week 5) | 5 of the easy benchmark requests reach 0 violations through the fix loop, all guardrails active |
| Benchmarks done (end of week 7) | All 70 requests run on 2 models; charts generated from `runs/` |

## Production questions and answers

**Each answer points to a part of this design.** Learn the reasoning, not the words.

**How does it scale to hundreds of developers?** A run is mostly waiting on the LLM, so one worker handles one run with little CPU. In Tier 2, requests go into a Redis queue and worker pods scale on queue length. The real limit is the LLM provider's rate limit, handled with a paid tier and a fallback provider.

**What if the model invents a field or an old API version?** Server-side dry-run sends the file to the real Kubernetes API, which rejects unknown fields and versions. That violation goes back to the fixer like any other.

**Can the fixer cheat, for example by deleting the resource that fails?** Yes, this is a real risk. Scanners pass an empty file. So the Gauntlet includes a fourth, deterministic check: **plan conformance**. Every resource in the approved plan must still exist with the same name, type, namespace, image and replica count. A fix that changes what the user approved is a violation.

**Why not let the agent run kubectl apply directly?** Three reasons: no human review, no history, no easy rollback. With GitOps, every change is a reviewed commit, and rolling back is a `git revert` that ArgoCD applies.

**What if someone edits the cluster by hand?** ArgoCD compares the cluster to Git and marks the app out of sync. With self-heal on, it restores the Git version. Git stays the single record of truth.

**Why rule engines instead of a second LLM as the judge?** A judge must give the same verdict every time and explain it with a rule ID. An LLM judge can be wrong, can be prompt-injected through the YAML, and cannot be audited. Deterministic checks can.

**Why not fine-tune a model?** We have no labelled dataset, and fine-tuning does not guarantee compliance. The feedback loop turns any capable model into a compliant one without training. The run logs could become a fine-tuning dataset later.

**The model is non-deterministic. How are your results valid?** Temperature is set to 0 to 0.2, each benchmark request runs 3 times, and we report the spread. The safety guarantee never depends on the model; it depends on the Gauntlet.

**What does one run cost?** We measure tokens per run in `metrics.tokens` and multiply by the provider's current price. On the free tier the cost is zero; we will quote a measured figure, not an estimate.

**What about false positives from the scanners?** Only `platform_admin` can skip a rule, and only with a written reason stored in the run log and the PR. Every skip is visible in review.

**How do you handle many teams?** One namespace per team, one OPA policy pack per role, and one ArgoCD project per team that can only deploy to its own namespaces.

**How do you test the tester?** Contract tests, unit tests on mocks, and a pass and fail example for every Rego rule, all in CI on every pull request.

## Risks and open decisions

**The biggest risk is the environment, not the code.** Settle the machine in week 1 before anyone writes features.

| Risk | Impact | Fallback |
| --- | --- | --- |
| Docker will not run in the chosen VM | Blocks the cluster, dry-run and ArgoCD | Switch to option A (WSL2) or B (Ubuntu VM) from the setup section |
| Free-tier LLM rate limits during benchmarks | Benchmark runs stall | Spread runs over several days; support a second provider behind `llm_service.py` |
| Free-tier model names or quotas change | Code breaks mid-project | Model name comes from `.env`, never hard-coded; record it in every run log |
| Fix loop fails on hard requests | Low final pass rate | Report it honestly as a finding; cap the hard tier; escalations are valid data |
| ArgoCD setup takes too long | Delays week 6 | Demo the PR step; apply merged files with kubectl by hand; ArgoCD is not on the critical path |
| One teammate falls behind | Their lane blocks a gate | Mocks keep the other lanes moving; re-split at each gate |

**Open decisions for the team:**

- [ ] Which machine runs the demo, and which setup option (A or B)
- [ ] Primary model and backup model for the comparison
- [ ] Add `"plan"` to the `Violation.tool` values in `contracts.py` for the plan-conformance check
- [ ] Whether to keep ArgoCD in scope or treat it as a stretch goal
