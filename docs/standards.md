# ACME Platform — Kubernetes Infrastructure Standards (sample)

This is a short, sample company standards document used as one source for the NL2Infra RAG
knowledge base. It is intentionally concrete so retrieval can ground the generator.

## Images
- Always pin images to a specific version tag (for example `nginx:1.27-alpine`). Never use
  `:latest` and never omit the tag. A digest is acceptable for reproducible builds.
- Prefer unprivileged base images (for example `nginxinc/nginx-unprivileged`) so the container
  can run as a non-root user.

## Security context
- Every container must run as non-root: set `securityContext.runAsNonRoot: true`.
- Never set `privileged: true`. Never request host namespaces (`hostNetwork`, `hostPID`, `hostIPC`)
  or `hostPath` mounts.
- In production, set `readOnlyRootFilesystem: true` and drop all Linux capabilities.
- Set `allowPrivilegeEscalation: false`.

## Resources
- Every container must declare CPU and memory limits (`resources.limits.cpu`,
  `resources.limits.memory`). Declare requests as well so the scheduler can place the pod.
- A sensible starting point is `cpu: 250m`, `memory: 256Mi` for a small web service.

## Secrets
- Never put a password, token, API key or private key as a literal value in an env var.
  Reference a Kubernetes `Secret` instead (`valueFrom.secretKeyRef`).

## Health and availability
- Deployments, StatefulSets and DaemonSets must define a `readinessProbe` so traffic is only
  sent to pods that are ready. A `livenessProbe` is recommended.
- Production Deployments and StatefulSets must run at least 2 replicas for availability.

## Namespaces and environments
- Developers deploy to `dev` and `staging`. Only admins may deploy to `prod`/`production`.
- Never deploy workloads into the `default` namespace; use a named namespace per environment.

## GitOps
- The AI never applies anything to the cluster. It only writes YAML. A human reviews and merges a
  pull request, and ArgoCD syncs the merged manifests. Git is the single source of truth.
