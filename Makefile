.PHONY: setup setup-env cluster namespaces deploy-argocd test run streamlit

export PATH := $(HOME)/.local/bin:$(PATH)

setup-env:
	bash setup_env.sh

setup:
	uv venv --python 3.12
	.venv/bin/python -m pip install -r requirements.txt

cluster:
	kind create cluster --name nl2infra --config infra/kind-config.yaml

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
