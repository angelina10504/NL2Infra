"""Phase 7 guards: the ArgoCD manifests and the manifests-repo template are well-formed,
and GitOpsService targets the configured separate repo."""
import os

import pytest

from src.nl2infra.gitops import GitOpsService

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
yaml = pytest.importorskip("yaml")


def _load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_argocd_applications_cover_all_three_environments():
    apps = {
        "dev": _load("infra/argocd/application.yaml"),
        "staging": _load("infra/argocd/application-staging.yaml"),
        "production": _load("infra/argocd/application-production.yaml"),
    }
    for env, app in apps.items():
        assert app["kind"] == "Application"
        assert app["spec"]["source"]["path"] == env
        assert app["spec"]["destination"]["namespace"] == env
        assert app["spec"]["project"] == "nl2infra"
    # dev/staging auto-sync; production is manual.
    assert "automated" in apps["dev"]["spec"]["syncPolicy"]
    assert "automated" in apps["staging"]["spec"]["syncPolicy"]
    assert "automated" not in (apps["production"]["spec"].get("syncPolicy") or {})


def test_argocd_project_whitelists_production():
    project = _load("infra/argocd/project.yaml")
    namespaces = {d["namespace"] for d in project["spec"]["destinations"]}
    assert {"dev", "staging", "production"} <= namespaces


def test_manifests_repo_template_has_env_folders():
    base = os.path.join(ROOT, "manifests-repo-template")
    assert os.path.isfile(os.path.join(base, "README.md"))
    for env in ("dev", "staging", "production"):
        assert os.path.isdir(os.path.join(base, env))


def test_gitops_targets_the_configured_manifests_repo(monkeypatch):
    monkeypatch.setenv("MANIFESTS_REPO", "acme/my-manifests")
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    assert GitOpsService(token="x").repo_name == "acme/my-manifests"


def test_gitops_local_fallback_without_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = GitOpsService(token="").commit_and_pr({"deployment.yaml": "kind: Deployment"},
                                                request_id="r9", environment="staging")
    assert url.startswith("file://")
    assert (tmp_path / "nl2infra-manifests" / "staging" / "r9" / "deployment.yaml").exists()
