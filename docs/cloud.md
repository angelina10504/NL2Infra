# Running NL2Infra on one cloud VM

The whole demo runs on one Linux VM: rootful Podman, kind, Argo CD, the compose stack from
`compose.yaml` and the ngrok agent. The local demo is unchanged.

```
browser --HTTPS--> ngrok --> app (127.0.0.1:8000) --PR--> manifests repo
                                 |                             | merge by a person
                                 | reads (dry run, status)     v
                                 +----------------------> kind <-- Argo CD
```

Only the app is public, through `infra/ngrok-team-policy.json`. Grafana, Prometheus,
Alertmanager, Jaeger, Argo CD and the Kubernetes API listen on the VM's loopback or inside its
private networks. SSH is allowed only through Google's Identity-Aware Proxy.

## Setup (on the VM, repository in `/opt/nl2infra`)

Prerequisites: `podman make git curl jq openssl acl docker-compose`, `kubectl`, `kind`, `ngrok`
and `systemctl enable --now podman.socket`.

```bash
cd /opt/nl2infra
sudo KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name nl2infra --config infra/kind-config.yaml
sudo make namespaces
sudo kubectl create namespace argocd
sudo kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.3/manifests/install.yaml
sudo make image
sudo ./infra/cloud/setup-access.sh                 # probe pod, app identity, secrets
sudo ./infra/cloud/configure.sh                    # asks for settings and secrets, hidden input
sudo ./infra/cloud/argocd.sh OWNER/MANIFESTS_REPO  # run, add the printed deploy key, run again
sudo cp infra/cloud/nl2infra.service infra/cloud/nl2infra-ngrok.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nl2infra nl2infra-ngrok
```

Set the OAuth app's callback to `<PUBLIC_BASE_URL>/auth/callback`. One ngrok domain serves one
agent at a time, so stop any tunnel on your laptop first.

## Credentials

| Credential | Scope | Where |
|---|---|---|
| App cluster identity | dry runs and reads in dev, staging, production, default; the Argo CD applications; exec in the probe pod | `secrets/app.kubeconfig`, readable by UID 10001 |
| Argo CD deploy key | read the manifests repository | `/etc/nl2infra/`, a secret in `argocd` |
| GitHub token | contents and pull requests on the manifests repository only | `.env`, mode 600 |
| ngrok authtoken | the tunnel | `/etc/nl2infra/ngrok.yml` |

An admission policy (`infra/cloud/app-access.yaml`) rejects every write from the app's identity
unless it is a server dry run. A generated resource kind that is not listed there cannot be
validated with this identity: the dry run reports a tool error and the run escalates.

## The Deploy stage

With `NL2INFRA_DEPLOY_OBSERVER=true` the app checks each unfinished pull request and records:

| Status | Observed |
|---|---|
| `waiting_review` | the pull request is open |
| `declined` | closed without merging |
| `syncing` | merged; Argo CD or Kubernetes is not there yet |
| `deployed` | Argo CD is `Synced` at the merge commit, workloads are fully ready, Services have ready endpoints and HTTP ports answer 200 |
| `failed` | the sync failed, the rollout exceeded its deadline, or `NL2INFRA_DEPLOY_TIMEOUT_SECONDS` passed |
| `superseded` | a later commit was synced before this one was verified |
| `awaiting_manual_sync` | merged to production, which a person syncs |
| `simulated` | mock mode: no pull request exists and nothing is deployed |

Final statuses are not re-checked: `deployed` records what was true at `checked_at`, not live
health. HTTP is probed on ports 80, 8080 and ports named `http*`; other ports need ready
endpoints only. Requests that produce the same file names overwrite each other in an environment
folder, and staging prunes, so the newest merged request is what runs.

## Switching on real runs

`NL2INFRA_MOCK_MODE=true` (the default) uses a mock model, mock validation checks and a mock
pull request. Setting it to `false` in `.env` makes real model calls and opens real pull
requests:

```bash
sudo systemctl restart nl2infra
```

## Observing from your own computer

```bash
gcloud compute ssh nl2infra-vm --project PROJECT --zone ZONE --tunnel-through-iap \
  -- -N -L 3000:127.0.0.1:3000
```

Grafana is then at <http://localhost:3000>.
