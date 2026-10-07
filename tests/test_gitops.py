import os
import pytest
from src.nl2infra.gitops import GitOpsService

def test_gitops_local_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = GitOpsService(token="", repo_name="test/repo")
    files = {
        "deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app"
    }
    pr_url = service.commit_and_pr(files, request_id="test1234")
    assert "file://" in pr_url
    assert "test1234" in pr_url
    
    # Verify file was written locally
    expected_file = tmp_path / "nl2infra-manifests" / "dev" / "test1234" / "deployment.yaml"
    assert expected_file.exists()
    assert "Deployment" in expected_file.read_text()
