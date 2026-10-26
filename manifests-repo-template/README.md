# nl2infra-manifests (template)

This is a **template for the separate GitOps repository** that NL2Infra deploys into. Copy its
contents into a new, empty GitHub repo (for example `nl2infra-manifests`), then point NL2Infra and
ArgoCD at that repo.

NL2Infra never applies anything to the cluster. It opens a pull request here with the generated,
Gauntlet-approved YAML. A human reviews and merges; ArgoCD then syncs the merged manifests.

## Structure

```
dev/          # ArgoCD syncs this folder to the dev namespace
staging/      # -> staging namespace
production/   # -> production namespace
```

NL2Infra writes files as `<env>/<kind>-<name>.yaml` on a branch `nl2infra/<request_id>` and opens a
PR against the default branch. Re-running the same request updates the branch instead of creating a
duplicate PR.

## How to use this template

```bash
# 1. Create an empty GitHub repo named nl2infra-manifests, then:
cp -r manifests-repo-template/* /path/to/nl2infra-manifests/
cd /path/to/nl2infra-manifests && git init && git add . && git commit -m "init" && git push

# 2. In the NL2Infra app .env, point it at the new repo and give it a token:
#    GITHUB_TOKEN=<a token with 'repo' scope>
#    MANIFESTS_REPO=<your-github-username>/nl2infra-manifests

# 3. Install ArgoCD on the kind cluster and apply the Applications (see docs/gitops.md).
```

See [`../docs/gitops.md`](../docs/gitops.md) in the main repo for the full end-to-end setup.
