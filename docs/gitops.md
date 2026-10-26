# GitOps & ArgoCD setup

NL2Infra never applies anything to the cluster. It opens a pull request in a **separate manifests
repository** with the generated, Gauntlet-approved YAML. A human reviews and merges the PR, and
ArgoCD syncs the merged manifests into the kind cluster. Git is the single source of truth.

```
NL2Infra app  --(PyGithub, GITHUB_TOKEN)-->  PR in nl2infra-manifests  --(merge)-->  ArgoCD  -->  kind
```

## 1. Create the manifests repository

Use the scaffold in [`../manifests-repo-template/`](../manifests-repo-template/):

```bash
# create an empty GitHub repo named nl2infra-manifests, then:
cp -r manifests-repo-template/* /path/to/nl2infra-manifests/
cd /path/to/nl2infra-manifests
git init && git add . && git commit -m "init manifests repo" && git branch -M main
git remote add origin https://github.com/<your-username>/nl2infra-manifests.git
git push -u origin main
```

It contains `dev/`, `staging/` and `production/` folders — one per environment ArgoCD watches.

## 2. Point NL2Infra at it

In the app's `.env`:

```bash
GITHUB_TOKEN=<a classic token with 'repo' scope, or a fine-grained token with contents+PR write>
MANIFESTS_REPO=<your-username>/nl2infra-manifests
```

With these set, `GitOpsService.commit_and_pr` pushes a branch `nl2infra/<request_id>` and opens a PR.
Without a token it falls back to writing files under `./nl2infra-manifests/<env>/<request_id>/` and
returns a `file://` path, so the pipeline still works offline.

## 3. Install ArgoCD on the kind cluster

```bash
make cluster        # kind create cluster --name nl2infra
make namespaces     # dev, staging, production (also needed for kubectl --dry-run=server)
make deploy-argocd  # installs ArgoCD into the argocd namespace
```

## 4. Wire ArgoCD to the manifests repo

Edit the `repoURL` in `infra/argocd/*.yaml` to your manifests repo, then apply:

```bash
kubectl apply -f infra/argocd/project.yaml
kubectl apply -f infra/argocd/application.yaml             # dev   (auto-sync)
kubectl apply -f infra/argocd/application-staging.yaml     # staging (auto-sync)
kubectl apply -f infra/argocd/application-production.yaml  # production (manual sync)
```

- `dev` and `staging` auto-sync (prune + self-heal).
- `production` has **no** automated sync block on purpose: a human promotes it by syncing in the
  ArgoCD UI or with `argocd app sync nl2infra-production`.

## 5. The end-to-end flow

1. A request passes guardrails, planning, approval, generation and the Gauntlet.
2. NL2Infra opens a PR in `nl2infra-manifests` and emits a `pr_opened` notification (dashboard bell).
3. A reviewer merges the PR.
4. ArgoCD detects the change and syncs it to the matching namespace.
5. Merging/sync can be surfaced back in the dashboard via `pr_merged` / `deploy_*` events (e.g. from a
   GitHub webhook or ArgoCD notification posting to `/alerts/webhook`).

## Access the ArgoCD UI

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
# initial admin password:
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```
