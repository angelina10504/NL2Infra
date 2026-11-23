# PHASE-2 — Containerise the app

Goal: one `Containerfile` that builds the NL2Infra app on Podman, runs as non-root, contains
the **exact** Gauntlet tool versions from the baseline, and passes the full test suite
**inside the container**, against the kind cluster.

No application behaviour changes. The compose stack is Phase 3. This phase is the app image only.

---

## Step 0 — Housekeeping from Phase 1

1. **Disk.** Report free space (`df -h ~`). If it's under **10 GB**, stop and ask me before
   building. Image builds fill the Podman VM's disk, and that disk lives on the Mac's.
2. **watchdog.** Add `watchdog==7.1.1 ; sys_platform == "linux"` to `requirements.lock`, with a
   comment that Streamlit needs it on Linux only. The environment marker keeps the Mac install
   unchanged.
3. **Docker kind cluster.** Leave it alone in this phase. I'll decide about it separately.

## Step 1 — Find out what the image must contain

Report before writing the Containerfile:
1. **Conftest.** Where the host binary comes from (`which conftest`, plus brew/go install
   info) and why it reports `dev`. Find a released Conftest version that embeds **OPA 1.19.0**.
   If none exists, stop and report. Don't pick a different OPA.
2. **kubectl.** We need v1.37.0 for Linux on this Mac's architecture (arm64).
3. **Every external binary or subprocess the Gauntlet calls** (`validators.py` and anything it
   imports), so nothing is missing at run time.
4. **Every path the app writes to at run time.** Known so far:
   - `data/auth.sqlite3` (`NL2INFRA_AUTH_DB`)
   - `events.jsonl` (`NL2INFRA_EVENTS_FILE`)
   - `nl2infra-manifests/` (relative to the working directory)

   Check for others, including wherever new runs are saved.
5. **Every path the app reads that isn't in git.** For example, `runs/` for `/research` and
   `/api/runs`. These get mounted read-only at run time and never copied into the image.

## Step 2 — Write the Containerfile

Requirements:
- **Base image** `python:3.12.13-slim-<debian release>`, pinned by **digest**. Python must be
  3.12.13.
- **Multi-stage build:**
  - `builder`: installs from `requirements.lock` into `/opt/venv` with uv.
  - `runtime`: the app.
  - `test`: `runtime` plus `tests/`, used only for running the suite.
- **kubectl v1.37.0 and Conftest.** Download each by exact version and **verify its SHA-256**
  against the checksum the project publishes. A checksum mismatch fails the build.
- **Non-root user.** Create `app` with UID/GID **10001** and set `USER 10001`. Every path the app
  writes to must be owned by 10001.
- **Environment.** Set `MODEL_NAME=openai/gpt-oss-120b`, `MOCK_MODE=true` (a real run
  overrides it at run time), `PYTHONDONTWRITEBYTECODE=1`, and `PYTHONUNBUFFERED=1`. Put the
  venv's `bin` on `PATH`.
- **Port and command.** `EXPOSE 8000`. `CMD`: uvicorn `src.nl2infra.server:app` on `0.0.0.0:8000`,
  without `--reload`.
- **HEALTHCHECK** against `GET /metrics`, which already exists. Don't add new routes.
- **Labels.** OCI labels for `org.opencontainers.image.source`, `revision` (git commit) and
  `version`.
- **`.containerignore`** must exclude at least: `.env*` (keep `.env.example`), `.venv*`, `.git`,
  `runs/`, `data/`, `events.jsonl`, `nl2infra-manifests/`, `test_artifacts/`, `CLAUDE.md`,
  `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.DS_Store`.

**No secret may enter the image.** Secrets reach the container at run time only, via
`--env-file .env`.

## Step 3 — Build

```
podman build -f Containerfile --target runtime -t nl2infra:dev .
podman build -f Containerfile --target test    -t nl2infra:test .
```
Report the build time and both image sizes.

## Step 4 — Prove the image is right

Run each check and show the output:
1. **User.** `podman run --rm nl2infra:dev id` prints uid=10001.
2. **Tool versions inside** must match the baseline exactly: Python 3.12.13, Checkov 3.3.26,
   OPA 1.19.0 (via Conftest), kubectl v1.37.0.
3. **Python packages.** `uv pip freeze` (or `pip freeze`) inside the image must equal
   `requirements.lock`, plus watchdog. Show the diff, which should be empty apart from watchdog.
4. **Rego.** `conftest verify --policy policies` inside the container must be 46/46.
5. **Full suite inside the container, against kind.** Steps:
   1. `kind get kubeconfig --internal --name nl2infra > <temp file outside the repo>`
   2. Run `nl2infra:test` with:
      - `--network kind`
      - that file mounted **read-only**, with `KUBECONFIG` pointing at it
      - `MOCK_MODE=true` and `GITHUB_TOKEN=` set empty
      - pytest's cache disabled (`-p no:cacheprovider`)
   3. It must be **422 passed**, including all 36 in `test_gauntlet_real.py`.

   Never mount a kubeconfig that points at `127.0.0.1`.
6. **App runs.** Start `nl2infra:dev` with:
   - `-p 8000:8000` and `--network kind`
   - the kubeconfig as above
   - `--env-file .env`
   - `--cpus 1 --memory 1g`
   - `MOCK_MODE=true`
   - `runs/` mounted read-only
   - a named volume for the writable paths

   Then check:
   - `curl -s localhost:8000/metrics | head` returns metrics
   - `/` returns 200
   - `/api/cluster` reaches the cluster
   - `podman inspect` reports the health status as healthy

   Stop and remove the container afterwards.
7. **No secrets in the image.**
   - `podman history --no-trunc nl2infra:dev`, checked for key-like strings (report yes or no
     only)
   - `.env` is absent from the image filesystem
   - `podman inspect` shows no secret environment variables baked into the image config

   Never print a value, even partially.
8. **Our own policies.** Run `checkov -f Containerfile --framework dockerfile`. Report every
   failed check, then fix it or explain why it doesn't apply.

## Step 5 — Make it one command

Add Makefile targets: `image` (build runtime), `image-test` (build test target and run the
suite in the container against kind), and `image-run` (start the app container as in step 6).
Existing targets must behave exactly as before.

## Step 6 — Checkpoint

1. The host suite must still pass: `MOCK_MODE=true GITHUB_TOKEN= .venv/bin/python -m pytest tests/ -q`
   gives 422 passed. Rego must be 46/46.
2. Write `docs/container.md`, covering:
   - how to build
   - how to run (the kubeconfig `--internal` trick and why)
   - the mounts and why
   - the measured sizes and versions
3. Propose a commit with `Containerfile`, `.containerignore`, `requirements.lock`, `Makefile`,
   `docs/container.md`, `AGENTS.md` and `PHASE-2.md`. **Ask me before committing and before
   pushing.**
4. Give the AGENTS.md §7 report and stop.
