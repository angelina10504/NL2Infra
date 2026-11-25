#!/usr/bin/env bash
# Brings the whole stack up after a boot: the kind node first, then compose. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/../.."
export DOCKER_HOST=unix:///run/podman/podman.sock

podman start nl2infra-control-plane >/dev/null
for _ in $(seq 90); do
  kubectl --kubeconfig /root/.kube/config get --raw /readyz >/dev/null 2>&1 && break
  sleep 2
done
kubectl --kubeconfig /root/.kube/config get --raw /readyz >/dev/null
docker-compose up -d --wait --wait-timeout 300
