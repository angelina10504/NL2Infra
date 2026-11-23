# The whole stack on Podman (`compose.yaml`)

`compose.yaml` runs the NL2Infra app and its monitoring (Prometheus, Alertmanager and Grafana)
as one Podman compose project called `nl2infra`. The OTel Collector and Jaeger come in Phase 5.

## Quick start

Before you start, you need the rootful Podman machine and the kind cluster from
[podman.md](podman.md), with Docker Desktop quit. You also need `.env` (app keys) and two
secret files in `secrets/` (see [Secrets](#secrets)).

```bash
make image PROVIDER=podman                 # app image nl2infra:dev (docs/container.md)
make kubeconfig-internal PROVIDER=podman   # kubeconfig that reaches kind by node name
make up                                    # checks secrets/, then podman compose up -d
```

`make ps` shows status, `make logs` shows the last 100 lines of each service, `make config`
prints the resolved config without `.env` values, and `make down` stops the stack.

## Services

| Service | What it is | URL (this Mac only) |
|---|---|---|
| `app` | NL2Infra dashboard and API; `/metrics` for Prometheus; `/alerts/webhook` for Alertmanager | http://127.0.0.1:8000 |
| `prometheus` | Scrapes `app:8000` every 10 s and evaluates the 3 alert rules (`observability/prometheus/`) | http://127.0.0.1:9090 |
| `alertmanager` | Routes firing alerts to `http://app:8000/alerts/webhook` with the bearer token | http://127.0.0.1:9093 |
| `grafana` | The "NL2Infra Overview" dashboard on the provisioned `http://prometheus:9090` datasource. Anonymous visitors are **Viewers**; the admin password comes from `.env`. | http://127.0.0.1:3000 |

The services reach each other by name on the compose network. The app also joins kind's
`kind` network, so it can reach the API server for the Gauntlet's dry-run.

## Hardening

| Service | User | Memory / CPU limit | Image pinned | Read-only rootfs | Caps dropped | Writable |
|---|---|---|---|---|---|---|
| app | 10001:10001 | 1 GiB / 1.0 | local build `nl2infra:dev` (base image pinned by digest in the Containerfile) | yes | ALL | volume `nl2infra-state`, tmpfs `/tmp` |
| alertmanager | 65534:65534 | 128 MiB / 0.25 | `v0.35.0@sha256:e638797f…` | yes | ALL | volume `alertmanager-data`, tmpfs `/tmp` |
| prometheus | 65534:65534 | 512 MiB / 0.5 | `v3.16.0@sha256:e0498764…` | yes | ALL | volume `prometheus-data`, tmpfs `/tmp` |
| grafana | 65534:65534 (not the image's 472) | 512 MiB / 0.5 | `13.3.1@sha256:0615ec4f…` | yes | ALL | volume `grafana-data`, tmpfs `/tmp` |

What applies to every service:
- `no-new-privileges`
- not privileged, and no host network
- a healthcheck
- ports published on `127.0.0.1` only

The data volumes are created owned by the service's user (Podman's `o: uid=…,gid=…`
volume option), so the containers can write to them without running as root.

All of this was checked with `podman inspect` and inside the containers:
- the cgroup `memory.max` and `cpu.max` values match the limits
- `CapEff` and `CapBnd` are 0
- `NoNewPrivs` is 1
- `id` shows the expected user

`tests/test_observability_config.py` enforces these rules on `compose.yaml`.

## Secrets

| Secret | Where it lives | How it gets in |
|---|---|---|
| App keys (Groq, GitHub, Slack, OAuth) | `.env` | `env_file: .env`, at run time only |
| Grafana admin password | `secrets/grafana_admin_password` (**not** in `.env`, so the app never sees it) | compose file secret, read only by `grafana` through `GF_SECURITY_ADMIN_PASSWORD__FILE` |
| Alert webhook token | `secrets/alert_webhook_token` | compose file secret, mounted into `app` and `alertmanager` at `/run/secrets/alert_webhook_token` |

Create the two secret files once. Neither command prints the secret:

```bash
mkdir -p secrets && chmod 700 secrets
(umask 077; openssl rand -hex 32 > secrets/alert_webhook_token)
(umask 077; read -rs -p "Grafana admin password: " p; printf '%s' "$p" > secrets/grafana_admin_password; unset p; echo)
```

`make up` refuses to start if either file is missing or empty (`make check-secrets`).

`.env` and `secrets/` are gitignored and excluded from the image build. The webhook accepts a
request only when the bearer token matches, compared in constant time. With no token
configured, it stays locked.

Two pitfalls:

- **`podman compose config` prints the app's `.env` values.** The compose provider expands
  `env_file: .env` into the app's environment in that output. Use `make config`, which runs
  `podman compose config --no-env-resolution`. Moving the app's keys to file secrets as well
  would need an app change; that's an open item.
- **Grafana falls back to `admin`/`admin` if the password file is missing.** Its start script
  only logs "can't open /run/secrets/grafana_admin_password" and carries on. Also, Grafana
  applies `GF_SECURITY_ADMIN_PASSWORD` only when it **creates** its database. So always start
  it through `make up`, which checks the file first. If the password ever needs changing in an
  existing `grafana-data` volume, write the new one to the file, then run:
  ```bash
  podman exec -i nl2infra-grafana-1 grafana cli --homepath /usr/share/grafana \
    admin reset-admin-password --password-from-stdin < secrets/grafana_admin_password
  ```

## Stopping

```bash
make down          # = podman compose down: removes containers and the network, keeps the volumes
```

The volumes `nl2infra-state`, `nl2infra_prometheus-data`, `nl2infra_alertmanager-data` and
`nl2infra_grafana-data` survive `down`/`up`. This was tested: Prometheus kept its earlier
samples, Grafana kept its password, and the app kept its events. `podman compose down -v`
would delete the volumes.

## Disk: run fstrim after pulling or building

The Podman VM's disk file on the Mac only grows. After `make image` or pulling images, run:

```bash
podman image prune -f
podman machine ssh -- sudo fstrim -a
```

## Measured (23 Nov 2026)

| Check | Result |
|---|---|
| `podman compose ps` | all 4 services healthy, 0 restarts |
| Prometheus targets | `nl2infra http://app:8000/metrics up`; `nl2infra_pending_approvals` returns data; 3 alert rules loaded |
| Grafana | 13.3.1, database ok; datasource "Successfully queried the Prometheus API"; dashboard "NL2Infra Overview" present |
| Anonymous Grafana | `/api/admin/settings`, `/api/org/users`, `POST /api/datasources` and `DELETE` dashboard all return 403. Admin with the `.env` password gets 200; `admin`/`admin` gets 403. |
| Alert path | test alert `NL2InfraTestAlert` → Alertmanager (1 sent, 0 failed) → `POST /alerts/webhook 200` → event `[NL2InfraTestAlert] TEST ALERT…` in the app's events store. Without the token, or with a wrong one: 401. |
| Mock run | `POST /api/run` needs a GitHub sign-in (401), so it wasn't triggered from here |
| Memory (`podman stats`) | app 81 MB, alertmanager 15 MB, prometheus 32 MB, grafana 287 MB, kind 655 MB: about 1.07 GB of the 4096 MB machine |
| `down` / `up` | all healthy again within 82 s; data kept (see Stopping) |
