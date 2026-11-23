# The NL2Infra app container

One `Containerfile` builds the app on Podman. The image runs as a non-root user, contains the
exact Gauntlet tool versions the recorded benchmark used, and passes the full test suite
inside the container against the kind cluster. It doesn't change any app behaviour.

Before you start, you need the rootful Podman machine and the kind cluster from
[podman.md](podman.md). With Docker Desktop quit, add `PROVIDER=podman` to every command that
talks to kind.

## Build

```bash
make image                       # runtime target → nl2infra:dev
make image-test PROVIDER=podman  # test target → nl2infra:test, then runs the suite in it
```

Both targets run `podman build --format docker`. Podman's default (OCI) image format silently
drops `HEALTHCHECK`, so the Docker format is needed.

The build has four stages:

| Stage | What it does |
|---|---|
| `builder` | `uv pip sync requirements.lock` into `/opt/venv`, so the image gets exactly the locked versions |
| `tools` | Downloads kubectl v1.37.0 and Conftest v0.69.0 and checks each against the SHA-256 its project publishes. A mismatch fails the build. |
| `runtime` | The app: `/opt/venv`, the two tools, and only the folders the app reads |
| `test` | `runtime` plus the rest of the build context (tests, fixtures, CI files…), used only to run the suite |

The base image is `python:3.12.13-slim-trixie`, pinned by tag **and** digest. The pin is
written out in each `FROM` line rather than through an `ARG`, so Checkov can see it.

## Run

```bash
make image-run PROVIDER=podman   # starts container nl2infra-app on http://localhost:8000
podman stop nl2infra-app && podman rm nl2infra-app
```

### Reaching kind from inside a container: `--internal`

Inside a container, `127.0.0.1` is the container itself. The normal kubeconfig points at
`https://127.0.0.1:<port>`, so it can't work there. Instead:

1. `make kubeconfig-internal PROVIDER=podman` runs `kind get kubeconfig --internal`. That
   writes a kubeconfig whose server is **`https://nl2infra-control-plane:6443`**, the kind
   node's name on kind's own network. It goes to `~/.kube/nl2infra-internal.config`, with
   mode 0600, outside the repo, because it holds cluster credentials.
2. The container joins that network (`--network kind`), so the node name resolves.
3. The file is mounted **read-only** at `/kube/config`, with `KUBECONFIG=/kube/config`.

Never mount a kubeconfig that points at `127.0.0.1`.

The dry-run checker has no cluster name hard-coded. It uses whatever context that kubeconfig
sets.

### Mounts and why

| Mount | Mode | Why |
|---|---|---|
| `~/.kube/nl2infra-internal.config` → `/kube/config` | read-only | cluster access (see above) |
| `runs/benchmark/` → `/app/runs/benchmark` | read-only | `/research` reads the recorded benchmark logs. They aren't in git and are never copied into the image. |
| named volume `nl2infra-state` → `/app/state` | read-write, owned by UID 10001 | all writable state, listed below |
| `runs/pilot/` → `/app/runs/pilot` | read-only, `image-test` only | two `test_ui.py` tests read the pilot logs |

Writable state, all inside the one volume:

| Path | Set by |
|---|---|
| `/app/state/runs/` | `NL2INFRA_RUNS_DIR`. New run logs and Slack approval updates. The host's live `runs/*.json` history is **not** shared, so the container's history starts empty. |
| `/app/state/auth.sqlite3` | `NL2INFRA_AUTH_DB`. Sign-in sessions and project members. A new volume has no members; `NL2INFRA_BOOTSTRAP_ADMIN_IDS` from `.env` recreates the admin. |
| `/app/state/events.jsonl` | `NL2INFRA_EVENTS_FILE` |
| `/app/nl2infra-manifests` → `/app/state/nl2infra-manifests` | symlink. The local PR fallback writes relative to the working directory, and that path has no environment variable. |

`/tmp` is the container's own, used for the Gauntlet's temporary files.

### Secrets

No secret is in the image. `.containerignore` keeps `.env*` (except `.env.example`), `.git`,
`runs/`, `data/`, `events.jsonl` and the other local state out of the build context.

Secrets arrive only at run time, through `--env-file .env`. `make image-run` then passes `-e`
flags **after** `--env-file`, which pin `MOCK_MODE=true` and the writable paths above,
whatever `.env` says.

The image sets `MODEL_NAME=openai/gpt-oss-120b` itself, so a container runs the benchmarked
model even without a `.env`.

### Run logs from the container

These fields differ from host runs. The app's behaviour doesn't change.

- **`commit`**: there's no git in the image, so `storage.git_commit()` falls back to
  `NL2INFRA_GIT_COMMIT`. The build sets it from the same `GIT_COMMIT` build argument as the
  `org.opencontainers.image.revision` label.
- **`tool_versions.conftest`**: `"0.69.0"` (the official release) instead of the host's `"dev"`
  (Homebrew's source build of the same 0.69.0). The embedded OPA is 1.19.0 in both.

## Measured (23 Nov 2026, Podman 6.2.0, Apple M2)

| | Result |
|---|---|
| Image size | `nl2infra:dev` 1.06 GB (1,061,874,184 bytes); `nl2infra:test` 1.06 GB (1,062,939,395 bytes) |
| Build time | first, cold build of runtime: 262 s; rebuilds with cache: 37–88 s; test target: 27–31 s |
| User | `uid=10001(app) gid=10001(app)` |
| Python / Checkov / Conftest / OPA / kubectl | 3.12.13 / 3.3.26 / 0.69.0 / 1.19.0 / v1.37.0 (baseline versions) |
| Python packages | identical to `requirements.lock`: 173 packages, including the Linux-only `watchdog==7.1.1` |
| Rego inside the image | 46 of 46 |
| Full suite inside the container, against kind | 426 passed, 0 skipped, including all 36 in `test_gauntlet_real.py` |
| App container (`--cpus 1 --memory 1g`) | healthy. `/metrics` and `/` return 200; about 80 MB RAM when idle. `/api/cluster` returns 401 without a GitHub sign-in, as on the host; `kubectl get nodes` inside the container shows the node Ready. |
| `checkov --framework dockerfile` | 159 passed, 0 failed (run on a copy named `Dockerfile`, because Checkov only recognises that file name) |

## Pitfalls

- **The Podman VM's disk file doesn't shrink on its own.** A build can grow it by several GB,
  even after the temporary files inside the VM are gone. On 23 Nov this filled the Mac's disk
  to 374 MB free. After builds, run:
  ```bash
  podman image prune -f                  # dangling build layers
  podman machine ssh -- sudo fstrim -a   # gives freed blocks back to macOS
  ```
- **kind picks Docker** whenever the `docker` CLI is installed. With Docker Desktop quit, pass
  `PROVIDER=podman`, or `kind get kubeconfig` fails with "failed to connect to the docker API".
- **The kind node doesn't restart with the Podman machine.** After `podman machine start`,
  run `podman start nl2infra-control-plane`. Tested on 23 Nov: the node came back Ready with
  its namespaces.
