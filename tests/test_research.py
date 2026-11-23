"""Research results use saved evaluator evidence and the existing membership boundary."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from eval.evaluate import arm_rows


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("NL2INFRA_BENCHMARK_DIR", str(tmp_path))
    return tmp_path


def save_result(folder, arm, request, *, safety=False, conformance=False, no_output=False,
                outcome="scored", missing_tokens=0, model="test-model"):
    verdict = {"safety_pass": safety, "conformance_pass": conformance,
               "all_four_pass": safety and conformance, "safety_violations": int(not safety)}
    log = {"arm": arm, "model_id": model, "temperature": 0.1, "commit": "test-commit",
           "benchmark": {"id": request, "repeat": 1},
           "summary": {"outcome": "no_output" if no_output else outcome,
                       "first": None if no_output else verdict,
                       "final": None if no_output else verdict,
                       "fix_rounds": int(arm == "C"), "runtime_seconds": 2.5,
                       "tokens_in": 100, "tokens_out": 50,
                       "calls_without_token_counts": missing_tokens}}
    (folder / f"{request}-{arm}.json").write_text(json.dumps(log))
    return log


def test_research_shell_contains_no_private_results_and_api_requires_sign_in():
    client = TestClient(server.app)
    assert client.get("/research").status_code == 200
    assert client.get("/api/research", headers={"X-Role": "admin"}).status_code == 401


@pytest.mark.parametrize("role", ["viewer", "developer", "admin"])
def test_members_get_evaluator_results_and_no_output_counts_as_failure(dataset, team_client, role):
    logs = [save_result(dataset, arm, "r1", safety=True, conformance=arm == "C") for arm in "ABC"]
    logs.append(save_result(dataset, "C", "r2", no_output=True))
    report = team_client(role).get("/api/research").json()
    assert report["available"] is True
    assert report["metadata"]["requests"] == 2
    assert report["metadata"]["repeats"] == 1
    c = report["arms"][2]
    assert c["rows"] == arm_rows([log for log in logs if log["arm"] == "C"])
    assert c["rows"]["All four checks pass"] == "50% (1/2)"
    assert c["no_output"] == 1
    assert report["arms"][1]["rows"]["Final pass rate"] == "100% (1/1)"
    assert report["arms"][1]["rows"]["All four checks pass"] == "0% (0/1)"
    assert report["warnings"]  # Different scored request sets are disclosed.


def test_results_are_recomputed_for_each_request(dataset, team_client):
    client = team_client()
    save_result(dataset, "A", "r1", safety=True, conformance=True)
    assert client.get("/api/research").json()["arms"][0]["rows"]["Runs scored"] == "1"
    save_result(dataset, "A", "r2")
    assert client.get("/api/research").json()["arms"][0]["rows"]["Runs scored"] == "2"


def test_empty_unscored_and_malformed_data_never_become_measured_results(dataset, team_client):
    client = team_client()
    report = client.get("/api/research").json()
    assert report["available"] is False
    assert all(not arm["rows"] and not arm["available"] for arm in report["arms"])
    save_result(dataset, "A", "r1", outcome="tool_crash")
    report = client.get("/api/research").json()
    assert report["available"] is False
    assert report["arms"][0]["unscored"] == {"tool_crash": 1}
    (dataset / "bad.json").write_text("{broken")
    report = client.get("/api/research").json()
    assert report["available"] is False
    assert "not measured" in report["message"]
    assert all(not arm["rows"] for arm in report["arms"])


def test_incomplete_token_accounting_is_not_measured(dataset, team_client):
    save_result(dataset, "C", "r1", safety=True, conformance=True, missing_tokens=1)
    report = team_client().get("/api/research").json()
    assert report["arms"][2]["rows"]["Median tokens (in + out)"] == "not measured"
    assert report["arms"][2]["rows"]["All four checks pass"] == "100% (1/1)"


def test_mixed_settings_duplicates_and_project_fit_are_not_pooled(dataset, team_client):
    first = save_result(dataset, "A", "r1")
    save_result(dataset, "B", "r1", model="another-model")
    client = team_client()
    assert client.get("/api/research").json()["available"] is False
    (dataset / "r1-B.json").unlink()
    (dataset / "duplicate.json").write_text(json.dumps(first))
    assert client.get("/api/research").json()["available"] is False
    (dataset / "duplicate.json").unlink()
    first["project_aware"] = True
    (dataset / "r1-A.json").write_text(json.dumps(first))
    report = client.get("/api/research").json()
    assert report["available"] is False
    assert "Project-fit" in report["message"]


def test_research_template_has_no_hardcoded_percentage_results():
    markup = (Path(server.TEMPLATE_PATH).parent / "research.html").read_text()
    visible = re.sub(r"<style\b[^>]*>.*?</style>|<script\b[^>]*>.*?</script>", "", markup,
                     flags=re.IGNORECASE | re.DOTALL)
    visible = re.sub(r"<[^>]+>", "", visible)
    assert not re.search(r"\b\d+(?:\.\d+)?\s*%", visible)
