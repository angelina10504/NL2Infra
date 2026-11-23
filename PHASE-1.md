# PHASE-1 — Lock the versions, then get kind running on Podman

Two parts, in order. **Part A must be committed before Part B starts.**

- **Part A — Reproducibility.** Pin what the benchmark depends on, so a container or a fresh
  machine runs the same Gauntlet. This answers Phase 0's open decisions.
- **Part B — Podman spike.** Install Podman, create a rootful machine, and run the kind
  cluster on it with Docker Desktop quit. If it fails, fall back cleanly and document why.

No application behaviour changes in this phase.

---

# Part A — Reproducibility

## A1. Lock file

1. Generate `requirements.lock` from the working `.venv`:
   `uv pip freeze --python .venv/bin/python > requirements.lock`
2. Diff it against the Phase 0 frozen list, if the scratchpad copy still exists. Report any difference.
3. Confirm it pins `checkov==3.3.26` and exact versions of `langgraph` and every `langchain-*`.
4. Add a comment line at the top: the lock was generated from the environment that produced
   the recorded benchmark (commit `aabb0d1`), and installs must use it.
5. Leave `requirements.txt` as it is, because it is the human-readable top-level list. Add one line
   to the README's setup section: "For a reproducible install use `requirements.lock`."
6. Flag any package in the lock that looks macOS-only, because Phase 2 builds a Linux image. Flag
   only; don't remove anything.

## A2. Pin the kind node image

1. Get the digest of the node image in use:
   `docker image inspect --format '{{index .RepoDigests 0}}' kindest/node:v1.37.0`
2. Set `image: kindest/node:v1.37.0@sha256:<digest>` on the node in `infra/kind-config.yaml`.
3. Don't recreate the cluster for this. Part B will use it.

## A3. Make the policy-coverage test ignore hidden files

This phase file allows one test change: in `test_every_rule_has_a_pass_and_a_fail_test`,
skip files whose name starts with `.` (for example `.DS_Store`). Don't change any other part of
the test or any policy. Make sure `.DS_Store` is in `.gitignore`.

Prove it works: put an empty `policies/.DS_Store` in place, run that test (it must pass),
then move the file back out.

## A4. Old virtualenv

`.venv.pre-move/` is no longer needed, because the new `.venv` is identical and passes all tests.
**Ask me before deleting it.**

## A5. Checkpoint

1. Full suite: `MOCK_MODE=true GITHUB_TOKEN= .venv/bin/python -m pytest tests/ -q`, which must
   be 422 passed. Then run `conftest verify --policy policies`, which must be 46/46.
2. Propose one commit: `requirements.lock`, README line, `infra/kind-config.yaml`, the test
   change, and `.gitignore` if it changed. **Ask me before committing.**
3. **Ask me before pushing.** I'll most likely say yes to pushing both `a56578a` and this commit.

---

# Part B — Podman spike

## B1. Before installing

Report:
- RAM: `sysctl -n hw.memsize`, in GB
- free disk: `df -h ~`
- CPU cores: `sysctl -n hw.ncpu`
- whether `docker-compose` or `podman-compose` is on PATH

Check how the dry-run checker picks its cluster (current kubectl context? the name
`kind-nl2infra`?) and report it.

## B2. Install Podman — I do this, not you

Stop and tell me to install it, with these instructions:
> Download the **official Podman installer (.pkg)** from https://podman.io (the Podman
> project recommends the installer over Homebrew). Run it, then tell Claude Code "installed".

After I confirm, verify `podman --version` and report it.

## B3. Create a rootful machine

kind on Podman needs a **rootful** machine. Pick memory from B1's results: 16 GB RAM → 6144 MB,
8 GB RAM → 4096 MB. Propose, then **ask me before running**:
```
podman machine init --rootful --cpus 4 --memory <MB> --disk-size 60
podman machine start
```
Verify: `podman info --format '{{.Host.Security.Rootless}}'` must be `false`, and
`podman run --rm quay.io/podman/hello` must work.

## B4. Hand over from Docker Desktop

1. Back up the kubeconfig: `cp ~/.kube/config ~/.kube/config.pre-podman`.
2. **Ask me to quit Docker Desktop.** Don't run Docker Desktop and the Podman VM together.
   Docker's kind cluster isn't deleted. It stops, and it's our fallback.

## B5. Create the cluster on Podman

```
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name nl2infra --config infra/kind-config.yaml
make namespaces
```
Record how long cluster creation took. Add a Makefile variable so this is one command, for example
`make cluster PROVIDER=podman`. Keep `make cluster` behaving exactly as it does today.

## B6. Prove it works with Docker Desktop quit

1. `docker ps` must fail or show nothing (Docker is off).
2. `podman ps` shows `nl2infra-control-plane`.
3. `kubectl get nodes` reports Ready v1.37.0, and `kubectl get ns` lists dev, staging and production.
4. `kubectl apply --dry-run=server -f samples/manifests/deployment.yaml` returns "(server dry run)".
5. Full test suite: 422 passed, **including every test in `test_gauntlet_real.py`**. Rego is 46/46.
6. Resource use: `podman stats --no-stream`, plus the machine's memory setting.
7. Report what `podman compose version` uses as its compose provider. Don't install one yet.

## B7. If it fails

Allow one clean retry (`podman machine stop && podman machine start`, then delete and recreate
the cluster). If it still fails:
- Stop. Don't hack around it.
- Restore the kubeconfig from `~/.kube/config.pre-podman`, and ask me to reopen Docker Desktop.
- Confirm that the Docker cluster works again and that the tests pass.
- Report the exact error.

## B8. Document

Write `docs/podman.md`:
- prerequisites
- install steps
- the machine settings and why it's rootful
- creating the cluster
- how to switch back to Docker (reopen Docker Desktop, then `kind export kubeconfig --name nl2infra`)
- what was measured in B6

If the spike failed, record the failure and the fallback instead.

## B9. Checkpoint

The full suite must pass. Propose a commit of `Makefile` and `docs/podman.md`.
**Ask before committing and before pushing.** Then give the AGENTS.md §7 report, with
Parts A and B as separate sections, and stop.
