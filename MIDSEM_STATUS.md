# NL2Infra mid-semester status

Date: 2026-10-08. Machine: macOS (arm64), local kind cluster `nl2infra`.

**Where it stands.** The pipeline now runs end to end on a real model and the three-arm benchmark works. On a pilot of 5 easy requests, the full pipeline (arm C) reached 0 violations on 5 of 5, against 2 of 5 for one call with the rule list (arm B) and 0 of 5 for one plain call (arm A). This is a pilot of 5 requests with 1 repeat each, so it shows the experiment runs; it is not a result to quote as a rate.

Test suite: **172 pytest tests passed, 0 failed, 0 skipped**; `conftest verify` passes **33 of 33** Rego tests. 28 of the pytest tests run the real Checkov, Conftest and cluster with nothing mocked.

---

## 1. Status table

| Component | Status | Tests passing | Note |
|---|---|---|---|
| Contracts (`contracts.py`) | Built | 14/14 | Rejects empty plans, empty names, workloads with no image, bad replicas, unknown severity |
| Gauntlet: Checkov | Built | covered in the 80 gauntlet tests | Bad or missing output is `TOOL_CRASH`; two rules skipped on purpose (section 4) |
| Gauntlet: OPA/Conftest | Built | 33/33 Rego, plus gauntlet tests | OPA 1.x syntax; 10 rules, each with a pass and a fail test; covers all workload kinds |
| Gauntlet: `kubectl --dry-run=server` | Built | covered in the gauntlet tests | Unreachable cluster is `TOOL_CRASH`; no client-side fallback |
| Gauntlet: plan conformance | Built | covered in the gauntlet tests | Kind, name, namespace, image, replicas; unplanned and duplicate resources |
| Gauntlet tests (`test_validators.py`, `test_gauntlet_real.py`) | Built | 80/80 | 52 unit, 28 against the real tools |
| LLM service (`llm_service.py`) | Built | 32/32 | Text only, no tools; temperature 0.1; 30 s timeout; backoff; waits on HTTP 429; tokens per call |
| Prompts (`prompts/`) | Built | covered in LLM service tests | Generate prompt carries the same rule list as arm B |
| Graph (`graph.py`) | Built | 27/27 | 8 nodes in the specified order; escalates at exactly 5 fix rounds; no node lets an error escape |
| Approval step | Built | covered in graph tests | Run pauses after planning; the confirmed plan is the Gauntlet's reference |
| Fix loop | Built | covered in graph and LLM tests | Only failing files re-sent, with rule ID, line and message; repeated rule includes the failed attempt |
| Run log (`storage.py`, `pipeline.py`) | Built | covered in graph tests | Every draft and violation per round, tokens per call, tool versions, timings |
| Benchmark runner (`benchmarks/run_benchmark.py`) | Built | 14/14 (with `evaluate.py`) | Arms A, B, C; `--arm --tier --limit --resume --dry-count`; sends nothing without `--yes` |
| Evaluation (`eval/evaluate.py`) | Built | in the 14 above | Counts beside every percentage; no charts yet |
| Guardrails (`guardrails.py`) | Partial | 4/4 | Now a graph node, but still the old keyword rules; no rate limit; role rules not enforced |
| Benchmark data | Partial | none | 5 of 30 correctness requests (all easy); 5 of 40 adversarial, still the original ones |
| Pull request (`gitops.py`) | Partial | 1/1 | Unchanged. The graph refuses to store a non-HTTP string as `pr_url`; real PR never run (`GITHUB_TOKEN` empty) |
| Deploy / ArgoCD (`infra/argocd/`) | Not built | none | Node only records `awaiting_merge`; placeholder repo URL; ArgoCD not installed |
| Manifests-repo workflow (`gauntlet.yml`) | Not built | none | |
| Streamlit UI (`app.py`) | Partial | 0 | Rewritten for the approval step; compiles; not opened in a browser |
| FastAPI server and web page | Partial | 0 | `/api/run` then `/api/approve` smoke-tested in mock mode; fake endpoints still present |
| README | Not built | none | |
| CI (`.github/workflows/ci.yml`) | Partial | none | Still pytest only; no ruff, no `conftest verify`; never run |
| Setup script (`setup_env.sh`) | Partial | none | Still Linux-only |

---

## 2. Pilot results

5 easy requests (`easy-01` to `easy-05`), 1 repeat, all three arms: 15 runs. Model `openai/gpt-oss-120b` on Groq, temperature 0.1.

How to read it:
- **Pass** means 0 violations from Checkov, OPA and server dry-run on the final files.
- **Plan conformance** is its own row and does not decide a pass. Arms A and B see only the English request, so their resource names differ from the plan.
- One planner call per request is shared by all three arms; its tokens are not counted in any arm.
- Arms B and C are given the same rule list. The only difference between them is the Gauntlet and fix loop.
- Guardrails were bypassed for these runs. The guardrails node would have refused 0 of the 5 requests.

| Metric | A: plain call | B: rules in prompt | C: full pipeline |
|---|---|---|---|
| Runs scored | 5 | 5 | 5 |
| First-draft pass rate | 0% (0/5) | 40% (2/5) | 0% (0/5) |
| Violations per request on draft 1 (mean) | 20.80 (n=5) | 0.60 (n=5) | 1.80 (n=5) |
| Final pass rate | 0% (0/5) | 40% (2/5) | 100% (5/5) |
| Mean fix rounds | 0 (n=5) | 0 (n=5) | 1.20 (n=5) |
| Plan conformance, final files | 0% (0/5) | 0% (0/5) | 100% (5/5) |
| All four checks pass | 0% (0/5) | 0% (0/5) | 100% (5/5) |
| Median runtime | 1.6 s (n=5) | 3.2 s (n=5) | 13.9 s (n=5) |
| Median tokens, in + out | 641 (n=5) | 2,711 (n=5) | 5,598 (n=5) |
| Median tokens in / out | 135 / 511 | 1,355 / 1,276 | 3,594 / 2,004 |

Runtime and tokens exclude the shared planning call and rate-limit waits. Arm C's runtime includes the Gauntlet (about 4 s per validation); arms A and B's is the model call alone.

**Arm C, violations per round** (all four checks; round 0 is the first draft):

| Round | Mean violations |
|---|---|
| 0 | 1.80 (n=5) |
| 1 | 0.20 (n=5) |
| 2 | 0.00 (n=1) |

**Arm C, rule IDs that needed fixing most often:**

| Rule | Occurrences over all rounds | Runs affected |
|---|---|---|
| `CKV_K8S_40` (run as a high UID) | 6 | 5/5 |
| `CKV_K8S_38` (service-account token mounted only where needed) | 4 | 4/5 |

Other observations:
- Arm B's three failures were each a single `CKV_K8S_40` violation, the same rule arm C had to fix in every run.
- Arm C's first draft was worse than arm B's (1.80 against 0.60 violations) although both get the same rules. Arm C writes one resource per call from the plan; arm B writes everything in one call. With 5 runs this may be noise.
- No final file in any arm contains an image digest. Every arm C image equals the planned image.
- No run was lost: 0 tool crashes, 0 model outages, 0 unparseable replies.
- Calls: 31 answered (5 plan, 5 arm A, 5 arm B, 10 generate, 6 fix), inside the 25 to 80 forecast. A further 22 attempts were refused with HTTP 429 (tokens-per-minute limit) and retried after waiting 188 s in total.
- Shared planner: median 891 tokens per plan (n=5).

Raw logs are in `runs/pilot/` (git-ignored): one JSON per run, the plans in `runs/pilot/plans/`, and `results.txt`.

Reproduce:

```bash
.venv/bin/python benchmarks/run_benchmark.py --tier easy --limit 5 --repeats 1 --arm all --out runs/pilot --yes
```

```bash
.venv/bin/python eval/evaluate.py --runs runs/pilot
```

---

## 3. Versions

| Item | Version |
|---|---|
| Model | `openai/gpt-oss-120b` (Groq), temperature 0.1 |
| Checkov | 3.3.26 |
| Conftest | `dev` build (Homebrew) |
| OPA | 1.19.0 |
| kubectl | v1.37.0 |
| Kubernetes (kind node) | v1.37.0 |
| Python | 3.12.13 |
| LangGraph | 1.2.14 |
| App commit | none: `git init` is done, nothing is committed yet, so run logs record `commit: null` |

---

## 4. Skipped Checkov rules

Both are skipped in `policies/checkov.yaml`, the one config the app uses, and are recorded in every run log.

| Rule | What it asks | Why it is skipped |
|---|---|---|
| `CKV_K8S_43` | Image must use a digest | The model cannot know a real digest, so the rule can only be "fixed" by inventing one (seen in the first audit). Image integrity is enforced instead by plan conformance (the image must equal the approved plan exactly), `CKV_K8S_14` (fixed tag, never `:latest`) and `CKV_K8S_15` (pull policy Always). A user who wants a digest writes it in the request. |
| `CKV2_K8S_6` | Every pod needs a NetworkPolicy | It can only clear by adding a NetworkPolicy, and a resource outside the approved plan is a conformance violation. Making the planner add one would change the tier sizes and fail arms A and B on every request for a structural reason. A NetworkPolicy the user asks for is planned and checked like any other resource. |

---

## 5. Left for after mid-sem

1. **Guardrails rewrite.** Scope, prompt-injection, role and per-user rate-limit checks without substring matching. Enforce the role rules (junior: dev only, no LoadBalancer, at most 2 replicas, no PVC; senior: dev and staging; admin: all).
2. **Benchmark data.** 25 more correctness requests (5 easy, 10 medium, 10 hard) and 40 adversarial requests (10 each of out-of-scope, injection, privilege escalation, wrong role), approved before any run.
3. **Pull request flow.** Real branch `nl2infra/<request_id>`, a description of what was asked, built and fixed, and no fake URL on a failed push. Needs a `GITHUB_TOKEN` and the manifests repo.
4. **Deploy.** ArgoCD application pointing at the real manifests repo, and the Gauntlet workflow in that repo.
5. **README, CI and cleanup.** Setup for macOS and Linux; CI with ruff, pytest and `conftest verify`; remove the hard-coded endpoints in `server.py`; open and check both UIs.
6. **Full benchmark.** 30 requests, 3 repeats, three arms, in batches, after the call count is approved. The free tier's tokens-per-minute limit refused 22 of 53 attempts in the pilot, so the full run will be slow.

Open points:
- Nothing is committed. The first commit should happen before the full benchmark so every run log carries a commit.
- Dry-run needs the target namespace to exist; only `dev`, `staging` and `production` do. Benchmark requests must stay within those.
- Checkov reports no severity in the open-source build, so every Checkov violation is recorded as `MEDIUM`.
