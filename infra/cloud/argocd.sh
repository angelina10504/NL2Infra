#!/usr/bin/env bash
# Run on the VM as root from the repository root: ./infra/cloud/argocd.sh OWNER/MANIFESTS_REPO
#
# First run: creates a read-only SSH deploy key, prints its PUBLIC half and stops.
# Add that key under the manifests repository's Settings > Deploy keys (leave "write" off).
# Second run: gives Argo CD the key and applies the staging (auto) and production (manual) apps.
# The dev app is deliberately not applied, so older manifests in dev/ are never synced.
set -euo pipefail
cd "$(dirname "$0")/../.."
repo=${1:?usage: argocd.sh OWNER/MANIFESTS_REPO}
[[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "not an OWNER/REPO name" >&2; exit 1; }
url="git@github.com:${repo}.git"
key=/etc/nl2infra/argocd_deploy_key

install -d -m 0755 /etc/nl2infra
if [ ! -f "$key" ]; then
  ssh-keygen -q -t ed25519 -N '' -C nl2infra-argocd-readonly -f "$key"
  echo "Add this PUBLIC key as a read-only deploy key on ${repo}, then run this script again:"
  cat "$key.pub"
  exit 0
fi

kubectl -n argocd create secret generic nl2infra-manifests-repo \
  --from-literal=type=git --from-literal=url="$url" --from-file=sshPrivateKey="$key" \
  --dry-run=client -o yaml |
  kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml |
  kubectl apply --server-side --force-conflicts -f - >/dev/null

sed "s#- '\*'\$#- '${url}'#" infra/argocd/project.yaml | kubectl apply -f -
for app in application-staging application-production; do
  sed "s#https://github.com/YOUR_GH_USERNAME/nl2infra-manifests.git#${url}#" "infra/argocd/$app.yaml" |
    kubectl apply -f -
done
echo ARGOCD_OK
