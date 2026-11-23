# PHASE-3 — One compose stack on Podman

Goal: `podman compose up -d` starts the whole system, which is the app plus Prometheus,
Alertmanager and Grafana. Every container follows our own policies, and monitoring works
end to end with the app **in a container**, not on the host.

The OTel Collector and Jaeger are **not** in this phase; they come in Phase 5. Leave room for
them, but don't add them. No application behaviour changes.

Decisions already made:
- Containers keep their own run history (named volume), as in Phase 2.
- Docker Desktop's disk stays until this phase passes.
- Compose uses the image from `make image`. Don't build inside compose, because the build
  needs `--format docker` to keep the HEALTHCHECK.

---

## Step 0 — Disk

Report free space. If it's under **5 GB**, stop and ask me. Run `fstrim` after pulling images
(see AGENTS.md §6).

## Step 1 — Plan before writing

Read `docker-compose.observability.yml`, everything in `observability/`, and the Makefile.
Report:
1. Every place that points at `host.docker.internal` or the host. This includes the Prometheus
   scrape target and the Alertmanager webhook.
2. The current Grafana settings: anonymous access, admin user, and datasource URL.
3. The image versions you'll pin. Use the current stable release of Prometheus, Alertmanager
   and Grafana, and pin each by **tag and digest**. If the dashboard JSON or alert rules need a
   particular version, say so.
4. Expected memory use. The Podman VM has 4096 MB, kind uses about 450 MB, and the app about
   80 MB. Propose a memory and CPU limit for every service.

## Step 2 — Write `compose.yaml`

One file at the repo root. Requirements for **every** service:
- **Image** pinned by tag and digest. Never `:latest`.
- **`user:`** set explicitly to a UID ≥ 10000. The app uses 10001; the official images' `nobody`
  (65534) is fine. Grafana's default (472) is not. Volumes must be writable by that UID (Podman's
  `:U` volume option, or set ownership another way, explained).
- **Limits:** a memory limit and a CPU limit. Verify they're really applied, because the docker-compose
  provider talks to Podman's socket: `podman inspect` must show them.
- **Hardening:** `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, and
  `read_only: true` with `tmpfs` where a service needs scratch space. If one of these breaks a
  service, drop only that setting for that service and give the reason.
- **No `privileged`**, and no host network.
- **Healthcheck** for each service. Use `depends_on` with `condition: service_healthy` where the
  order matters.
- **Ports** published on `127.0.0.1` only: 8000 (app), 9090, 9093 and 3000.

Service-specific:
- **app:** image `nl2infra:dev`.
  - Secrets: `env_file: .env`, injected at run time only.
  - Networks: the compose network plus the external `kind` network.
  - Kubeconfig: `~/.kube/nl2infra-internal.config`, read-only, via a variable with that default.
  - Volumes: `runs/benchmark` and `runs/pilot` read-only, plus the `nl2infra-state` volume.
- **prometheus:** scrapes `app:8000`. Keep the existing alert rules unchanged.
- **alertmanager:** the webhook goes to `http://app:8000/alerts/webhook`.
- **grafana:**
  - Anonymous access becomes **Viewer**, not Admin.
  - The admin password comes from `GRAFANA_ADMIN_PASSWORD` in `.env`. Tell me to add it; never
    set or print it yourself.
  - The provisioned datasource is `http://prometheus:9090`, and the existing dashboard is
    provisioned as now.

Change the configs in `observability/` to use service names instead of the host.
**Move** `docker-compose.observability.yml` to `infra/legacy/` with a comment at the top saying
it is the old Docker-only stack and what replaced it. Don't delete it.

## Step 3 — Bring it up

```
make image PROVIDER=podman        # only if the image is out of date
make kubeconfig-internal PROVIDER=podman
podman compose up -d
```
Add Makefile targets: `up`, `down`, `ps` and `logs` (using `podman compose`). Existing targets
must stay unchanged.

## Step 4 — Prove monitoring works end to end

Run each and show the output:
1. **Health.** `podman compose ps` shows every service healthy.
2. **Limits.** `podman inspect` shows the memory and CPU limit for each container, and its user.
3. **Scraping.** Prometheus `/api/v1/targets` shows the app target `up`, and a query for one
   NL2Infra metric returns data.
4. **Grafana.** The API shows the datasource healthy and the NL2Infra dashboard present.
   Anonymous access is Viewer only: try an admin-only API call without logging in and show
   that it is refused.
5. **Alert path, end to end.** Post one synthetic test alert to Alertmanager's API
   (`/api/v2/alerts`). Show that it reaches the app's webhook and appears in the app's events
   (`/events`, or the events store). Label it clearly as a test alert.
6. **Activity shows up.** Trigger one mock run through the app (`MOCK_MODE=true`, no real LLM
   call) if the API allows it without a GitHub sign-in. Show the related counter changing in
   Prometheus. If sign-in blocks this, report it and don't work around auth.
7. **Memory.** `podman stats --no-stream` with everything up, and the total against 4096 MB.
8. **Restart.** Run `podman compose down`, then `podman compose up -d`. Everything comes back healthy,
   and Grafana and Prometheus data survive (the volumes persist).
9. **Not leaking.** `podman compose config` must not show secret values. Check by key names
   only. Don't print the output whole.

## Step 5 — Checkpoint

1. Host suite: 426 passed. `make image-test PROVIDER=podman`: 426 passed. Rego: 46/46.
2. Write `docs/compose.md`:
   - quick start (3–4 commands)
   - what each service is and its URL
   - the hardening table: service × user / limits / pinned / read-only / caps dropped
   - how to stop
   - the fstrim note
3. Update the README quick start to point at it.
4. Propose a commit (`compose.yaml`, `observability/`, `infra/legacy/`, Makefile, docs,
   README, `AGENTS.md`, `PHASE-3.md`). **Ask me before committing and before pushing.**
5. Ask me whether to clear Docker Desktop's data now that the stack runs on Podman.
   Don't do it yourself.
6. Give the AGENTS.md §7 report and stop.
