"""UI tests: the run-log logic in ui_data, and the Streamlit app driven headless in mock mode and replay mode.

No LLM calls and no cluster: live runs use MOCK_MODE=true and replay reads saved logs.
"""
import json
import os

import pytest

import ui_data as ui

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "src", "nl2infra", "app.py")


def violation(rule, tool="checkov", file="deployment-web.yaml", line=1, resource="Deployment.dev.web"):
    return {"tool": tool, "rule_id": rule, "severity": "MEDIUM", "file": file, "line": line, "message": f"{rule} failed",
            "resource": resource}


def make_log(status="passed", rounds=None, **extra):
    rounds = rounds if rounds is not None else [
        {"round": 0, "files": {"deployment-web.yaml": "kind: Deployment\nspec:\n  replicas: 1\n", "service-web.yaml": "kind: Service\n"},
         "violations": [violation("CKV_K8S_40"), violation("CKV_K8S_38"), violation("OPA_NO_ROOT", tool="opa", line=3)]},
        {"round": 1, "files": {"deployment-web.yaml": "kind: Deployment\nspec:\n  replicas: 1\n  runAsUser: 10001\n", "service-web.yaml": "kind: Service\n"},
         "violations": [violation("CKV_K8S_40"), violation("CKV_K8S_22")]},
        {"round": 2, "files": {"deployment-web.yaml": "kind: Deployment\nspec:\n  replicas: 1\n  runAsUser: 10001\n  ro: true\n", "service-web.yaml": "kind: Service\n"},
         "violations": []},
    ]
    plan = {"resources": [{"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.27-alpine", "replicas": 1}}]}
    log = {"request_id": "req1", "status": status, "final_status": status, "prompt": "Deploy nginx", "role": "junior_dev",
           "model_id": "fake-model", "plan": plan, "approved_plan": plan, "rounds": rounds,
           "iterations": max(len(rounds) - 1, 0), "rejection_reason": None, "pr_url": None, "pr_error": None,
           "metrics": {"total_time": 12.3, "tokens_in": 1000, "tokens_out": 400}}
    log.update(extra)
    return log


# ---------------------------------------------------------------- ui_data

def test_timeline_of_a_passed_run():
    steps = ui.timeline(make_log())
    assert [s["kind"] for s in steps] == ["round", "round", "round", "end"]
    assert [s["label"] for s in steps] == ["Draft 1", "Fix 1", "Fix 2", "Passed"]
    assert [s["count"] for s in steps[:3]] == [3, 2, 0]
    assert steps[0]["tools"] == {"checkov": 2, "opa": 1, "dry-run": 0, "plan": 0}
    assert steps[-1]["passed"] is True


@pytest.mark.parametrize("status,reason,label", [
    ("escalated", "max_fix_rounds: 1 violation(s) remain after 5 fix rounds", "Escalated"),
    ("rejected", "off_topic: Request does not seem related to infrastructure.", "Rejected"),
    ("running", None, "Running"),
])
def test_timeline_never_says_passed_unless_the_log_does(status, reason, label):
    end = ui.timeline(make_log(status=status, rejection_reason=reason))[-1]
    assert end["label"] == label and end["passed"] is False
    assert end["reason"] == (reason or "")


def test_timeline_of_runs_that_never_produced_a_draft():
    rejected = ui.timeline(make_log(status="rejected", rounds=[], plan=None, approved_plan=None,
                                    rejection_reason="off_topic: not infrastructure"))
    assert [(s["kind"], s["label"]) for s in rejected] == [("stage", "Guardrails"), ("end", "Rejected")]
    declined = ui.timeline(make_log(status="rejected", rounds=[], approved_plan=None,
                                    rejection_reason="plan_not_approved: the user did not confirm the plan"))
    assert declined[0]["label"] == "Approval"
    no_plan = ui.timeline(make_log(status="escalated", rounds=[], plan=None, approved_plan=None,
                                   rejection_reason="bad_llm_output: plan rejected twice"))
    assert no_plan[0]["label"] == "Plan"
    assert ui.timeline(make_log(status="running", rounds=[]), awaiting_approval=True)[-1]["label"] == "Awaiting approval"


def test_timeline_shows_a_fix_round_that_failed_to_produce_a_draft():
    log = make_log(status="escalated", rejection_reason="llm_unavailable: fix: timed out")
    log["rounds"] = log["rounds"][:2]
    log["iterations"] = 2
    steps = ui.timeline(log)
    assert [(s["kind"], s["label"]) for s in steps] == [("round", "Draft 1"), ("round", "Fix 1"), ("stage", "Fix 2"), ("end", "Escalated")]


def test_violation_rows_status_and_order():
    rounds = make_log()["rounds"]
    first = ui.violation_rows(rounds, 0)
    assert {r["status"] for r in first} == {"new in this round"} and len(first) == 3
    second = ui.violation_rows(rounds, 1)
    assert [(r["rule_id"], r["status"]) for r in second] == [
        ("CKV_K8S_40", "still failing"), ("CKV_K8S_22", "new in this round"),
        ("CKV_K8S_38", "fixed in this round"), ("OPA_NO_ROOT", "fixed in this round"),
    ]
    third = ui.violation_rows(rounds, 2)
    assert [(r["rule_id"], r["status"]) for r in third] == [("CKV_K8S_22", "fixed in this round"), ("CKV_K8S_40", "fixed in this round")]


def test_diff_rows_are_aligned():
    rows = ui.diff_rows("a\nb\nc\nd\n", "a\nB\nc\nd\ne\n")
    assert [(r["left_no"], r["right_no"], r["tag"]) for r in rows] == [
        (1, 1, "equal"), (2, 2, "changed"), (3, 3, "equal"), (4, 4, "equal"), (None, 5, "added")]
    assert ui.diff_stats(rows) == (1, 2)
    removed = ui.diff_rows("a\nb\nc\n", "a\nc\n")
    assert [(r["left_no"], r["right_no"], r["tag"]) for r in removed] == [(1, 1, "equal"), (2, None, "removed"), (3, 2, "equal")]
    assert all(r["tag"] == "equal" for r in ui.diff_rows("same\n", "same\n"))


def test_changed_files_and_gutter_marks():
    rounds = make_log()["rounds"]
    assert ui.changed_files(rounds[0]["files"], rounds[1]["files"]) == ["deployment-web.yaml"]
    assert ui.gutter_marks(rounds[0]["violations"], "deployment-web.yaml") == {1: ["CKV_K8S_40", "CKV_K8S_38"], 3: ["OPA_NO_ROOT"]}
    assert ui.gutter_marks(rounds[0]["violations"], "service-web.yaml") == {}
    assert ui.unplaced_rules([violation("X", line=None)], "deployment-web.yaml") == ["X"]


def test_run_log_listing_and_benchmark_folders(tmp_path):
    (tmp_path / "pilot" / "plans").mkdir(parents=True)
    (tmp_path / "pilot" / "_pipeline").mkdir()
    (tmp_path / "abc.json").write_text(json.dumps(make_log()))
    (tmp_path / "pilot" / "easy-01-A-r1.json").write_text(json.dumps(make_log(summary={
        "outcome": "scored", "fix_rounds": 0, "final": {"safety_pass": False, "safety_violations": 20, "conformance_violations": 2}})))
    (tmp_path / "pilot" / "easy-01-C-r1.json").write_text(json.dumps(make_log()))
    (tmp_path / "pilot" / "plans" / "easy-01-r1.json").write_text("{}")
    (tmp_path / "pilot" / "_pipeline" / "easy-01-C-r1.json").write_text("{}")
    (tmp_path / "pilot" / "notes.json").write_text('{"not": "a run log"}')

    logs = ui.list_run_logs(str(tmp_path))
    assert set(logs) == {"abc.json", os.path.join("pilot", "easy-01-A-r1.json"), os.path.join("pilot", "easy-01-C-r1.json"),
                         os.path.join("pilot", "notes.json")}
    assert ui.load_log(str(tmp_path / "pilot" / "notes.json")) is None
    assert ui.benchmark_folders(str(tmp_path)) == ["pilot"]
    requests = ui.benchmark_requests(str(tmp_path / "pilot"))
    assert list(requests) == ["easy-01 (repeat 1)"] and set(requests["easy-01 (repeat 1)"]) == {"A", "C"}
    verdict = ui.arm_verdict(ui.load_log(requests["easy-01 (repeat 1)"]["A"]))
    assert (verdict["passed"], verdict["violations"], verdict["conformance_violations"]) == (False, 20, 2)
    assert ui.arm_verdict(None) == {"available": False}


# ---------------------------------------------------------------- the app, headless

def page_text(app) -> str:
    return " ".join(str(element.value) for element in app.markdown)


@pytest.fixture
def app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("NL2INFRA_RUNS_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    st.cache_resource.clear()
    return AppTest.from_file(APP, default_timeout=60), tmp_path


def test_live_run_plan_then_approve_shows_the_loop(app):
    at, runs = app
    at.run()
    assert not at.exception
    assert "No plan yet" in page_text(at)

    at.text_area(key="request").set_value("Deploy a server to the dev namespace")
    at.button(key="submit").click().run()
    assert not at.exception
    text = page_text(at)
    assert "mock-deploy" in text and "nginx:1.27-alpine" in text           # the plan table
    assert "Fix loop" not in text and not list(runs.glob("*.json"))        # nothing generated before approval

    at.button(key="approve").click().run()
    assert not at.exception
    text = page_text(at)
    assert "Draft 1 &rarr; Gauntlet" in text and "Fix 1 &rarr; Gauntlet" in text
    assert '<div class="status">Passed</div>' in text
    assert "PLAN APPROVED" in text                                         # the request row collapsed to one line
    assert "fixed in this round" in text and "CKV_K8S_1" in text           # violations table for the last round
    assert "Final status" in text and "PASSED" in text
    assert len(list(runs.glob("*.json"))) == 1


def test_live_run_rejected_plan_shows_the_reason_and_no_pass(app):
    at, _ = app
    at.run()
    at.text_area(key="request").set_value("Deploy a server to the dev namespace")
    at.button(key="submit").click().run()
    at.button(key="reject").click().run()
    assert not at.exception
    text = page_text(at)
    assert '<div class="status">Rejected</div>' in text
    assert "plan_not_approved" in text
    assert "Passed" not in text and "PASSED" not in text
    assert not at.error


def test_guardrail_refusal_is_shown_in_the_timeline_not_as_an_error(app):
    at, _ = app
    at.run()
    at.text_area(key="request").set_value("Write a poem about sunflowers")
    at.button(key="submit").click().run()
    assert not at.exception and not at.error
    text = page_text(at)
    assert '<div class="lab">Guardrails</div>' in text and "off_topic" in text
    assert "Passed" not in text


def replay(at, runs, log, name="saved.json"):
    (runs / name).write_text(json.dumps(log))
    at.run()
    at.radio(key="mode").set_value("Replay a saved run").run()
    return page_text(at)


def test_replay_needs_no_api_key_and_shows_diff_and_statuses(app, monkeypatch):
    at, runs = app
    monkeypatch.setenv("MOCK_MODE", "false")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("KUBECONFIG", "/nonexistent")
    text = replay(at, runs, make_log())
    assert not at.exception
    assert "fake-model" in text
    assert '<div class="status">Passed</div>' in text and "After 2 fix round(s)" in text
    # last round is selected: diff of fix 1 -> fix 2, and its violations
    assert "Before · Fix 1" in text and "After · Fix 2" in text
    assert '<td class="ad">  ro: true</td>' in text
    assert "saved.json" in text and "1,000" in text

    at.button(key="view_Replay a saved run_req1_1").click().run()
    text = page_text(at)
    assert "Before · Draft 1" in text and '<td class="ad">  runAsUser: 10001</td>' in text
    assert "CKV_K8S_40 CKV_K8S_38" in text                                  # gutter marks on the left panel
    assert "still failing" in text and "new in this round" in text and "fixed in this round" in text

    at.button(key="view_Replay a saved run_req1_2").click().run()
    at.radio(key="compare_Replay a saved run_req1").set_value("first draft").run()
    assert "Before · Draft 1" in page_text(at) and "After · Fix 2" in page_text(at)


def test_replay_escalated_run_shows_reason_and_never_passed(app):
    at, runs = app
    log = make_log(status="escalated", rejection_reason="max_fix_rounds: 2 violation(s) remain after 5 fix rounds")
    log["rounds"] = log["rounds"][:2]
    text = replay(at, runs, log)
    assert not at.exception and not at.error
    assert '<div class="status">Escalated</div>' in text and "max_fix_rounds: 2 violation(s) remain" in text
    assert "Passed" not in text and "PASSED" not in text


def test_replay_single_draft_run_says_so(app):
    at, runs = app
    log = make_log()
    log["rounds"] = [dict(log["rounds"][2], round=0)]
    log["iterations"] = 0
    text = replay(at, runs, log)
    assert not at.exception
    assert "This run has only one draft. It passed the Gauntlet first time." in text
    assert "First draft, no fix needed" in text and "Before ·" not in text


def test_yaml_is_escaped(app):
    at, runs = app
    log = make_log()
    log["rounds"][2]["files"]["deployment-web.yaml"] += "  note: <script>alert(1)</script> $HOME\n"
    text = replay(at, runs, log)
    assert "<script>" not in text and "&lt;script&gt;" in text and "&#36;HOME" in text


def test_compare_arms_tab_reads_saved_logs(app):
    at, runs = app
    (runs / "bench").mkdir()
    summary = {"outcome": "scored", "fix_rounds": 0, "final": {"safety_pass": False, "safety_violations": 20, "conformance_violations": 2}}
    (runs / "bench" / "easy-01-A-r1.json").write_text(json.dumps(make_log(status="escalated", summary=summary, arm="A")))
    passed = {"outcome": "scored", "fix_rounds": 2, "final": {"safety_pass": True, "safety_violations": 0, "conformance_violations": 0}}
    (runs / "bench" / "easy-01-C-r1.json").write_text(json.dumps(make_log(summary=passed, arm="C")))
    at.run()
    assert not at.exception
    text = page_text(at)
    assert '<div class="verdict bad">Fail</div>' in text and "20 violation(s) · 2 off-plan · 0 fix round(s)" in text
    assert '<div class="verdict ">Pass</div>' in text and "0 violation(s) · 0 off-plan · 2 fix round(s)" in text
    assert "No saved run for this arm." in text                             # arm B is missing
