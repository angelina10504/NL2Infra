#!/usr/bin/env bash
# Run on the VM as root from the repository root, after the kind cluster exists.
# Creates the probe pod, the app's restricted identity, its kubeconfig and the compose secrets.
# Prints no secret values.
set -euo pipefail
cd "$(dirname "$0")/../.."

kubectl apply -f infra/cloud/probe.yaml
kubectl apply -f infra/cloud/app-access.yaml
kubectl -n nl2infra-probe rollout status deploy/probe --timeout=240s

install -d -m 0711 secrets
mkdir -p runs/benchmark runs/pilot

token=""
for _ in $(seq 30); do
  token=$(kubectl -n nl2infra-system get secret nl2infra-app-token -o jsonpath='{.data.token}' | base64 -d)
  [ -n "$token" ] && break
  sleep 2
done
[ -n "$token" ] || { echo "the service account token was not issued" >&2; exit 1; }
ca=$(kubectl -n nl2infra-system get secret nl2infra-app-token -o jsonpath='{.data.ca\.crt}')

( umask 077; cat > secrets/app.kubeconfig <<KUBE
apiVersion: v1
kind: Config
clusters:
- name: nl2infra
  cluster:
    server: https://nl2infra-control-plane:6443
    certificate-authority-data: ${ca}
users:
- name: nl2infra-app
  user:
    token: ${token}
contexts:
- name: nl2infra
  context: {cluster: nl2infra, user: nl2infra-app}
current-context: nl2infra
KUBE
)
chown 10001:10001 secrets/app.kubeconfig
chmod 0400 secrets/app.kubeconfig

# Created once; re-running keeps the existing values. Readable only by the container users.
for name in alert_webhook_token grafana_admin_password; do
  [ -s "secrets/$name" ] || ( umask 077; openssl rand -hex 24 | tr -d '\n' > "secrets/$name" )
done
chmod 0400 secrets/alert_webhook_token secrets/grafana_admin_password
setfacl -m u:10001:r,u:65534:r secrets/alert_webhook_token
setfacl -m u:65534:r secrets/grafana_admin_password

make --no-print-directory observability/.local/busybox
echo ACCESS_OK
