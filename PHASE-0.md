# PHASE-0 — Sync and baseline

Goal: a clean, committed, measured starting point before any Podman or telemetry work.
**Change no application behaviour in this phase.** The only edits allowed are the two
config fixes in Step 3.

Already confirmed: the repo is at `~/dev/NL2Infra`, local `main` = `origin/main` = `148d23e`,
Docker Desktop is running, no kind cluster exists, Podman is not installed.

---

## Step 1 — Check the move from iCloud lost nothing

1. Look for files iCloud evicted: `find . -name "*.icloud" -not -path "./.git/*"`.
   Any result means a missing file. List them and stop.
2. Count files in the gitignored run logs (they exist nowhere else):
   `runs/benchmark`, `runs/benchmark_hard_partial`, `runs/benchmark_extra_repeats`, `runs/pilot`.
   Expected: 60, 17, 66, 15. Report any difference.
3. `.env`: list only the key **names** that are set. Never print values.
4. `.venv` was built at the old iCloud path and stores absolute paths, so it's probably broken.
   Test it with `.venv/bin/python -c "import fastapi, langgraph; print('ok')"`.
   If it fails, propose `uv venv --python 3.12 .venv && uv pip install -r requirements.txt`.
   **Ask me before running it.**

## Step 2 — Which model produced the recorded benchmark?

Without printing any secret, check what model the run logs in `runs/benchmark/` record
(grep for a model field). Report the model name(s) and how many runs used each.
Expected: all `openai/gpt-oss-120b`. If not, stop and report: it changes what the
benchmark claims.

## Step 3 — Settle the uncommitted work

For each changed file, give a one-line summary of the diff. Then propose these actions and
**ask me before editing anything**:

| File | Proposed action |
|---|---|
| `observability/prometheus/prometheus.yml` | Revert the scrape target from `8001` back to `8000`. The app port is 8000 everywhere else. If the diff contains anything besides the port, show it and keep it. |
| `docs/temporary-sharing.md` | Change `ngrok http 8001` to `ngrok http 8000`. |
| `.env.example` | Set `MODEL_NAME=openai/gpt-oss-120b`, so a fresh checkout or a container runs the benchmarked model. Change nothing else. |
| `server.py`, `templates/index.html`, `templates/research.html`, `tests/test_research.py` | The `/research` page — keep as is and commit, if tests pass. |
| `infra/ngrok-team-policy.json` | Keep and commit. |
| `AGENTS.md`, `PHASE-0.md` | Commit, so teammates see the plan. |

Do **not** change the code default model in `llm_service.py`. Just note it in the report.

## Step 4 — Recreate the kind cluster

Check the `Makefile` targets `cluster` and `namespaces`, and confirm the cluster name they
create matches what the dry-run checker expects. Show me what they do, then
**ask me before running** `make cluster && make namespaces`.

## Step 5 — Baseline

Run and record, with output:
1. Full test suite: `MOCK_MODE=true GITHUB_TOKEN= .venv/bin/python -m pytest tests/ -q`
2. Rego tests: `conftest verify --policy policies`
3. Tool versions: python, checkov, conftest, opa, kubectl, kind, docker.
4. Cluster: `kind get clusters`, `kubectl get nodes`, and
   `kubectl apply --dry-run=server -f samples/manifests/deployment.yaml`
   (expect "(server dry run)").
5. Running containers: `docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'`.
6. Every `:latest` image in `docker-compose.observability.yml`. List only; don't change.

If any test fails, stop and report. Don't fix anything in this phase.

## Step 6 — Commit (only after I approve)

Propose one commit with the files from Step 3 and a message describing them.
**Ask me before committing, and again before pushing.**
Never commit `.env`, `CLAUDE.md`, `runs/`, `.venv/` or anything else `.gitignore` covers.

## Step 7 — Report

Use the AGENTS.md §7 format. Include the full baseline numbers, because later phases are
compared against them. Then stop.
