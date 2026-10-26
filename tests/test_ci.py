"""Phase 6 guards: the CI workflow declares the required gates, and the AI PR reviewer
is comment-only and fails soft (it never blocks a PR and never calls the model without a key)."""
import importlib.util
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
yaml = pytest.importorskip("yaml")


def _load_workflow():
    with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_ci_runs_on_pull_requests():
    wf = _load_workflow()
    on = wf.get(True) or wf.get("on")  # PyYAML parses the bare key `on:` as boolean True
    assert "pull_request" in on


def test_ci_has_all_required_gates():
    wf = _load_workflow()
    steps = " ".join(str(s.get("run", "")) + " " + str(s.get("name", ""))
                     for s in wf["jobs"]["test"]["steps"])
    assert "ruff" in steps                         # lint
    assert "pytest" in steps                        # tests
    assert "conftest verify" in steps               # Rego tests
    assert "checkov" in steps                        # Checkov on sample manifests
    assert "run_benchmark.py --dry-count" in steps   # smoke benchmark


def test_ai_review_job_is_comment_only_and_pr_scoped():
    wf = _load_workflow()
    job = wf["jobs"]["ai-review"]
    assert "pull_request" in job["if"]
    assert job["permissions"]["pull-requests"] == "write"
    # It must not grant itself any merge/approve capability beyond commenting.
    assert job["permissions"]["contents"] == "read"


def _load_review_module():
    path = os.path.join(ROOT, ".github", "scripts", "ai_review.py")
    spec = importlib.util.spec_from_file_location("ai_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_review_fails_soft_without_key_or_diff(monkeypatch, capsys):
    for var in ("GROQ_API_KEY", "GH_TOKEN", "PR_NUMBER", "BASE_SHA", "HEAD_SHA"):
        monkeypatch.delenv(var, raising=False)
    module = _load_review_module()
    assert module.main() == 0                        # never blocks the PR
    assert "AI risk review" in capsys.readouterr().out
