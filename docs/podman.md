# Running kind on Podman (macOS)

The Gauntlet's server-side dry-run needs a kind cluster. This page runs that cluster on
Podman instead of Docker Desktop. It was tested on 23 Nov 2026 on an Apple M2 MacBook Air
with 8 GB RAM, with Docker Desktop quit.

## Prerequisites

- macOS on Apple silicon. The steps should also work on Intel Macs, but that hasn't been tested.
- About 6 GB of free disk. The Podman machine image used about 3 GB. Pulling the kind node
  image into the machine grew its disk to about 3.5 GB.
- `kind` v0.33.0 and `kubectl` v1.37.0 on PATH, as for the Docker setup.
- **Quit Docker Desktop first.** Don't run Docker Desktop and the Podman machine together
  on an 8 GB laptop.

## Install Podman

Install the **Podman engine** from the official installer `.pkg` on https://podman.io
(Download → macOS). It installs into `/opt/podman/bin` and adds that folder to PATH for new
terminals through `/etc/paths.d`.

Podman Desktop is only a GUI and doesn't include the engine. You can keep it installed, but
don't let it create a machine for you, because its default is rootless.

```bash
podman --version        # tested with: podman version 6.2.0
```

## Create the machine

kind on Podman needs a **rootful** machine. kind's node container has to manage its own
cgroups, mounts and networking, which a rootless Podman setup doesn't allow.

```bash
# 8 GB Mac: 4096 MB. 16 GB Mac: 6144 MB.
podman machine init --rootful --cpus 4 --memory 4096 --disk-size 60
podman machine start
```

`--disk-size 60` is the maximum size of the machine's disk. The space isn't reserved up front;
the disk file grows as it fills.

Check it:

```bash
podman info --format '{{.Host.Security.Rootless}}'   # must print: false
podman run --rm quay.io/podman/hello                  # must print the Podman greeting
```

## Create the cluster

```bash
make cluster PROVIDER=podman   # = KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster ...
make namespaces                # dev, staging, production
```

Plain `make cluster` still uses Docker, as before.

`infra/kind-config.yaml` pins the node image by digest
(`kindest/node:v1.37.0@sha256:a1ed56cf…`), so Docker and Podman run the same Kubernetes
build that the recorded benchmark used.

The dry-run checker uses the **current kubectl context** and has no cluster name hard-coded.
`kind create cluster --name nl2infra` sets that context to `kind-nl2infra` with either
provider.

## Switching back to Docker

The Docker cluster is never deleted. Quitting Docker Desktop only stops it.

1. Make a backup first, if you don't have one: `cp ~/.kube/config ~/.kube/config.pre-podman`.
2. `podman machine stop`
3. Open Docker Desktop and wait until it's running.
4. `kind export kubeconfig --name nl2infra`. Run it **without** `KIND_EXPERIMENTAL_PROVIDER`,
   so kind talks to Docker. This points `kind-nl2infra` back at the Docker cluster.
5. `kubectl get nodes` should show `nl2infra-control-plane Ready`.

Both clusters are called `nl2infra` and use the same context name, so whichever one you
exported last is the one kubectl and the Gauntlet use.

To go back to Podman: quit Docker Desktop, `podman machine start`, then
`KIND_EXPERIMENTAL_PROVIDER=podman kind export kubeconfig --name nl2infra`.

## Measured (23 Nov 2026, Docker Desktop quit)

| Check | Result |
|---|---|
| `docker ps` | fails: the Docker Desktop socket doesn't exist |
| `podman ps` | `nl2infra-control-plane`, image `kindest/node@sha256:a1ed56cf…` |
| `kubectl get nodes` | `nl2infra-control-plane Ready control-plane v1.37.0` |
| `kubectl get ns` | includes dev, staging, production |
| `kubectl apply --dry-run=server -f samples/manifests/deployment.yaml` | `deployment.apps/web created (server dry run)` |
| Full test suite (`MOCK_MODE=true`) | 422 passed, including all 36 in `test_gauntlet_real.py` |
| `conftest verify --policy policies` | 46 of 46 |
| Cluster creation time | 1401 s, almost all of it the first pull of the node image (about 1.3 GB unpacked). The image is cached after that. |
| Node container (`podman stats`, right after start) | 452 MB RAM of the 4.07 GB machine limit, 160 PIDs |
| Machine | rootful, 4 CPUs, 4096 MB RAM, 60 GB disk, libkrun |
| `podman compose` | Has no compose provider of its own. It runs the external `/usr/local/bin/docker-compose` (Docker Compose v5.5.0, installed with Docker Desktop). |

## Not tested yet

- Whether the kind cluster survives `podman machine stop` / `start`, or the laptop sleeping.
  If it doesn't, recreate it with `make cluster PROVIDER=podman`. The node image is cached,
  so that's much faster than the first time.
- Running `make cluster PROVIDER=podman` itself. The cluster above was created with the
  equivalent `kind` command, and the Makefile target was only checked with `make -n`.
- Switching back to Docker, as described above.
