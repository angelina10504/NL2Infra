.PHONY: setup setup-env cluster namespaces deploy-argocd test run streamlit image image-test image-run kubeconfig-internal

export PATH := $(HOME)/.local/bin:$(PATH)

setup-env:
	bash setup_env.sh

setup:
	uv venv --python 3.12
	.venv/bin/python -m pip install -r requirements.txt

# `make cluster` uses kind's default provider (Docker). `make cluster PROVIDER=podman`
# runs kind on a rootful Podman machine instead; see docs/podman.md.
PROVIDER ?=
cluster:
	$(if $(PROVIDER),KIND_EXPERIMENTAL_PROVIDER=$(PROVIDER) )kind create cluster --name nl2infra --config infra/kind-config.yaml

# Server dry-run needs every namespace a role can target to exist.
namespaces:
	for ns in dev staging production; do kubectl create namespace $$ns --dry-run=client -o yaml | kubectl apply -f -; done

deploy-argocd:
	kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

test:
	.venv/bin/python -m pytest tests/ -v

run:
	.venv/bin/python -m uvicorn src.nl2infra.server:app --host 0.0.0.0 --port 8000 --reload

streamlit:
	.venv/bin/python -m streamlit run src/nl2infra/app.py

# ---- Container image (Podman; see docs/container.md) --------------------------------------
# --format docker keeps the HEALTHCHECK (Podman's default OCI format drops it).
IMAGE_BUILD = podman build --format docker -f Containerfile \
	--build-arg GIT_COMMIT=$$(git rev-parse HEAD) --build-arg VERSION=$$(git describe --tags --always --dirty)
# Kubeconfig for containers on kind's network: the API server by node name, never 127.0.0.1.
# It holds cluster credentials, so it lives outside the repo, readable by its owner only.
KUBECONFIG_INTERNAL ?= $(HOME)/.kube/nl2infra-internal.config
CONTAINER_KUBE = --network kind -v $(KUBECONFIG_INTERNAL):/kube/config:ro -e KUBECONFIG=/kube/config

image:
	$(IMAGE_BUILD) --target runtime -t nl2infra:dev .

kubeconfig-internal:
	umask 077; $(if $(PROVIDER),KIND_EXPERIMENTAL_PROVIDER=$(PROVIDER) )kind get kubeconfig --internal --name nl2infra > $(KUBECONFIG_INTERNAL)

image-test: kubeconfig-internal
	$(IMAGE_BUILD) --target test -t nl2infra:test .
	podman run --rm $(CONTAINER_KUBE) -e MOCK_MODE=true -e GITHUB_TOKEN= \
		-v $(CURDIR)/runs/pilot:/app/runs/pilot:ro nl2infra:test

# Secrets come from .env at run time only. The -e flags after it pin mock mode and the
# writable paths, whatever .env says.
image-run: kubeconfig-internal
	podman run -d --name nl2infra-app -p 8000:8000 $(CONTAINER_KUBE) \
		--cpus 1 --memory 1g --env-file .env \
		-e MOCK_MODE=true -e NL2INFRA_RUNS_DIR=/app/state/runs \
		-e NL2INFRA_AUTH_DB=/app/state/auth.sqlite3 -e NL2INFRA_EVENTS_FILE=/app/state/events.jsonl \
		-v $(CURDIR)/runs/benchmark:/app/runs/benchmark:ro -v nl2infra-state:/app/state \
		nl2infra:dev
