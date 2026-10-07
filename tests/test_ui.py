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
    # line 1 is only where the resource starts, so it is never tagged
    assert ui.gutter_marks(rounds[0]["violations"], "deployment-web.yaml") == {3: ["OPA_NO_ROOT"]}
    assert ui.gutter_marks(rounds[0]["violations"], "service-web.yaml") == {}
    assert ui.gutter_marks([violation("X", line=None), violation("Y", line=1)], "deployment-web.yaml") == {}
    assert ui.file_failures(rounds[0]["violations"], "deployment-web.yaml") == [
        ("CKV_K8S_40", "CKV_K8S_40 failed"), ("CKV_K8S_38", "CKV_K8S_38 failed"), ("OPA_NO_ROOT", "OPA_NO_ROOT failed")]
    assert ui.where(violation("X", line=1)) == "Deployment.dev.web"
    assert ui.where(violation("X", line=None, resource=None)) == "-"
    assert ui.where(violation("X", line=7)) == "line 7"


def test_default_file_is_the_first_with_violations_then_the_first_changed():
    names = ["deployment.yaml", "service.yaml", "configmap.yaml"]
    assert ui.default_file(names, [violation("X", file="service.yaml"), violation("Y", file="configmap.yaml")], []) == "service.yaml"
    assert ui.default_file(names, [], ["configmap.yaml"]) == "configmap.yaml"
    assert ui.default_file(names, [violation("X", file="configmap.yaml")], ["service.yaml"]) == "configmap.yaml"
    assert ui.default_file(names, [], []) == "deployment.yaml"
    assert ui.default_file(names, [violation("X", file=None)], []) == "deployment.yaml"


def test_difference_only_drops_the_instruction_to_the_fixer():
    assert ui.difference_only("Namespace/dev in manifests.yaml is not in the approved plan. Remove it; do not add resources.") == \
        "Namespace/dev in manifests.yaml is not in the approved plan."
    assert ui.difference_only("Deployment/x image mismatch: the plan says ['a:1'], the file says ['a:2']. Use exactly the planned "
                              "image string. Do not change the tag, add a digest or add containers.") == \
        "Deployment/x image mismatch: the plan says ['a:1'], the file says ['a:2']."
    assert ui.difference_only("Service/x is declared more than once; keep one definition.") == "Service/x is declared more than once."
    assert ui.difference_only("Planned resource Service/x (namespace: dev) is missing from the generated files.") == \
        "Planned resource Service/x (namespace: dev) is missing from the generated files."


def bench_log(request_id, arm, prompt, repeat=1, **extra):
    return make_log(prompt=prompt, arm=arm, benchmark={"id": request_id, "arm": arm, "repeat": repeat, "tier": "easy"}, **extra)


def test_arm_consistency_catches_logs_from_different_requests():
    good = {arm: bench_log("easy-02", arm, "Deploy nginx") for arm in "ABC"}
    assert ui.arm_consistency("easy-02 (repeat 1)", good) == ("easy-02", [])
    assert ui.arm_consistency("easy-02 (repeat 1)", {"A": good["A"], "B": None}) == ("easy-02", [])
    wrong_id = dict(good, C=bench_log("easy-01", "C", "Deploy nginx"))
    assert "arm C log is for easy-01" in ui.arm_consistency("easy-02 (repeat 1)", wrong_id)[1][0]
    wrong_text = dict(good, B=bench_log("easy-02", "B", "A Redis cache"))
    assert ui.arm_consistency("easy-02 (repeat 1)", wrong_text)[1] == ["the arm logs hold different request texts"]
    assert ui.arm_consistency("easy-02 (repeat 1)", dict(good, A=bench_log("easy-02", "A", "Deploy nginx", repeat=2)))[1]
    assert ui.arm_consistency("nonsense", good)[0] is None


def test_compact_rows_keep_three_lines_of_context_and_true_line_numbers():
    before = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"
    after = before.replace("line 10\n", "line ten\n").replace("line 30\n", "line 30\nextra\n")
    rows = ui.compact_rows(ui.diff_rows(before, after), context=3)
    shape = [("skip", r["count"]) if r["tag"] == "skip" else (r["left_no"], r["right_no"], r["tag"]) for r in rows]
    assert shape == [
        ("skip", 6),
        (7, 7, "equal"), (8, 8, "equal"), (9, 9, "equal"), (10, 10, "changed"), (11, 11, "equal"), (12, 12, "equal"), (13, 13, "equal"),
        ("skip", 14),
        (28, 28, "equal"), (29, 29, "equal"), (30, 30, "equal"), (None, 31, "added"), (31, 32, "equal"), (32, 33, "equal"), (33, 34, "equal"),
        ("skip", 7),
    ]
    # hunks closer than the context merge, and an unchanged file is one divider
    near = ui.compact_rows(ui.diff_rows("a\nb\nc\nd\ne\n", "A\nb\nc\nd\nE\n"), context=3)
    assert [r["tag"] for r in near] == ["changed", "equal", "equal", "equal", "changed"]
    assert ui.compact_rows(ui.diff_rows("a\nb\n", "a\nb\n")) == [{"tag": "skip", "count": 2}]


def test_plain_wording_never_shows_an_exception_name():
    assert ui.plain_reason("internal_error: RuntimeError: disk full") == "internal_error: disk full"
    assert ui.plain_reason("llm_unavailable: plan: RateLimitError: Error code: 429") == "llm_unavailable: plan: Error code: 429"
    assert ui.plain_reason(None) == ""
    assert ui.pull_request_note({"pr_url": "https://github.com/a/b/pull/1"}) == ("Pull request: https://github.com/a/b/pull/1", "")
    assert ui.pull_request_note({"pr_error": "RuntimeError: publishing is disabled for benchmark runs"}) == (
        "Pull request not opened: benchmark run", "")
    assert ui.pull_request_note({"pr_error": "file:///x/nl2infra-manifests/dev/abc (Local GitOps simulated: set GITHUB_TOKEN)"}) == (
        "Pull request not opened: no GitHub token", "")
    assert ui.pull_request_note({"pr_error": "GithubException: 403 Forbidden"}) == ("Pull request not opened", "403 Forbidden")


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
    assert '<div class="nl-request">Deploy a server to the dev namespace</div>' in text   # the request, in full
    assert [e.label for e in at.expander] == ["Plan approved: 2 resources"]
    assert "Pull request: https://github.com/mock/repo/pull/" in text
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
    assert "Pull request not opened" in text and "Error" not in text
    assert not [w for w in at.selectbox if w.key == "role"]                # no role selector in replay
    assert "Role (saved run)" in text and "junior_dev" in text
    assert [e.label for e in at.expander] == ["Plan approved: 1 resource"]

    at.button(key="view_Replay a saved run_req1_1").click().run()
    text = page_text(at)
    assert "Before · Draft 1" in text and '<td class="ad">  runAsUser: 10001</td>' in text
    assert "Draft 1 failed:" in text and "<b>CKV_K8S_40</b> &ndash; CKV_K8S_40 failed" in text   # strip above the diff
    assert '<td class="n hit" title="OPA_NO_ROOT">3</td>' in text          # a real line (3) is marked on its number
    assert '<td class="n">1</td>' in text and "CKV_K8S_40 CKV_K8S_38" not in text      # line 1 is not
    assert "<th>Rule</th>" not in text and 'class="g"' not in text         # no gutter column
    assert "<th>Line or resource</th>" in text and "Deployment.dev.web" in text
    assert "still failing" in text and "new in this round" in text and "fixed in this round" in text

    at.button(key="view_Replay a saved run_req1_2").click().run()
    at.radio(key="compare_Replay a saved run_req1").set_value("first draft").run()
    assert "Before · Draft 1" in page_text(at) and "After · Fix 2" in page_text(at)


def test_replay_diff_defaults_to_changed_hunks_with_a_full_file_toggle(app):
    at, runs = app
    body = "".join(f"  key{i}: value{i}\n" for i in range(1, 41))
    log = make_log()
    log["rounds"] = [
        {"round": 0, "files": {"configmap.yaml": "kind: ConfigMap\ndata:\n" + body}, "violations": [violation("CKV_K8S_21", file="configmap.yaml")]},
        {"round": 1, "files": {"configmap.yaml": "kind: ConfigMap\ndata:\n" + body.replace("value20", "changed")}, "violations": []},
    ]
    log["iterations"] = 1
    text = replay(at, runs, log)
    assert not at.exception
    assert at.radio(key="view_mode_Replay a saved run_req1").value == "Changes only"
    assert "&middot;&middot;&middot; 18 unchanged lines" in text and "&middot;&middot;&middot; 17 unchanged lines" in text
    assert '<td class="n">22</td><td class="rm">  key20: value20</td>' in text   # true line number kept
    assert "key5: value5" not in text and "key19: value19" in text and "key23: value23" in text

    at.radio(key="view_mode_Replay a saved run_req1").set_value("Full file").run()
    text = page_text(at)
    assert "unchanged lines" not in text and "key5: value5" in text and "key40: value40" in text


def test_each_round_opens_on_the_first_file_with_violations(app):
    at, runs = app
    files = {"deployment.yaml": "kind: Deployment\n", "service.yaml": "kind: Service\n", "configmap.yaml": "kind: ConfigMap\n"}
    log = make_log()
    log["rounds"] = [
        {"round": 0, "files": files, "violations": [violation("CKV_K8S_21", file="service.yaml"), violation("X", file="configmap.yaml")]},
        {"round": 1, "files": dict(files, **{"service.yaml": "kind: Service\nfixed: true\n", "configmap.yaml": "kind: ConfigMap\nfixed: true\n"}),
         "violations": []},
    ]
    log["iterations"] = 1
    replay(at, runs, log)
    # fix 1 is selected: the left-hand draft's first failing file is service.yaml, not the last file
    assert at.radio(key="file_Replay a saved run_req1_1_previous round").value == "service.yaml  (changed)"
    at.button(key="view_Replay a saved run_req1_0").click().run()
    assert at.radio(key="file_Replay a saved run_req1_0_previous round").value == "service.yaml"
    assert "<b>CKV_K8S_21</b>" in page_text(at)


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
    summary = {"outcome": "scored", "fix_rounds": 0, "tokens_in": 300, "tokens_out": 600, "runtime_seconds": 1.6,
               "final": {"safety_pass": False, "safety_violations": 20, "conformance_violations": 2}}
    arm_a = make_log(status="escalated", summary=summary, arm="A")
    arm_a["rounds"] = [{"round": 0, "files": {"manifests.yaml": "kind: Deployment\n"}, "violations": [
        violation("CKV_K8S_23", file="manifests.yaml"), *[violation(f"RULE_{n}", file="manifests.yaml") for n in range(1, 7)],
        {"tool": "plan", "rule_id": "PLAN_CONFORMANCE_UNPLANNED", "severity": "HIGH", "file": "manifests.yaml", "line": 1,
         "message": "Deployment/redis-cache in manifests.yaml is not in the approved plan. Remove it; do not add resources.",
         "resource": "Deployment/redis-cache"}]}]
    arm_a["benchmark"] = {"id": "easy-01", "arm": "A", "repeat": 1}
    (runs / "bench" / "easy-01-A-r1.json").write_text(json.dumps(arm_a))
    passed = {"outcome": "scored", "fix_rounds": 2, "final": {"safety_pass": True, "safety_violations": 0, "conformance_violations": 0}}
    (runs / "bench" / "easy-01-C-r1.json").write_text(json.dumps(
        make_log(summary=passed, arm="C", benchmark={"id": "easy-01", "arm": "C", "repeat": 1})))
    at.run()
    assert not at.exception
    text = page_text(at)
    assert '<div class="verdict bad">Fail</div>' in text and "20 violation(s) · 2 off-plan · 0 fix round(s)" in text
    assert '<div class="verdict ">Pass</div>' in text and "0 violation(s) · 0 off-plan · 2 fix round(s)" in text
    assert "No saved run for this arm." in text                             # arm B is missing
    assert "<th>Arm</th><th>Verdict</th><th>Violations</th><th>Off-plan</th><th>Fix rounds</th><th>Tokens</th><th>Runtime</th>" in text
    assert '<td class="st-fail">Fail</td><td class="m">20</td><td class="m">2</td><td class="m">0</td><td class="m">900</td><td class="m">1.6 s</td>' in text
    assert "Off-plan: resources or values that differ from the shared reference plan." in text
    # arm A's violations are listed under its verdict; its off-plan items separately
    assert '<td class="m nl-red">CKV_K8S_23</td><td class="m">Checkov</td><td>CKV_K8S_23 failed</td>' in text
    assert "Off-plan (1)" in text and "PLAN_CONFORMANCE_UNPLANNED" in text
    assert "<td>Deployment/redis-cache in manifests.yaml is not in the approved plan.</td>" in text
    assert "Remove it; do not add resources" not in text
    assert "Arms A and B never see the plan, so a different resource name counts as off-plan. Off-plan does not affect pass or fail." in text
    assert "<b>REQUEST easy-01</b>" in text
    # 7 violations: the first 5 are listed, all 7 are behind the expander
    assert [e.label for e in at.expander if e.label.startswith("Show all")] == ["Show all 7"]
    assert text.count("<td>CKV_K8S_23 failed</td>") == 2 and text.count("<td>RULE_6 failed</td>") == 1
    assert "No violations from Checkov, OPA or dry-run." in text            # arm C
    assert [e.label for e in at.expander if e.label == "Show final files"] == ["Show final files"] * 2
    assert text.count('<div class="nl-code fixed">') == 2


def test_compare_arms_refuses_to_mix_requests(app):
    at, runs = app
    (runs / "bench").mkdir()
    (runs / "bench" / "easy-02-A-r1.json").write_text(json.dumps(bench_log("easy-02", "A", "Deploy nginx")))
    (runs / "bench" / "easy-02-C-r1.json").write_text(json.dumps(bench_log("easy-01", "C", "A Redis cache")))   # wrong request
    at.run()
    assert not at.exception
    text = page_text(at)
    assert "These logs do not belong to one request" in text and "arm C log is for easy-01" in text
    assert "<th>Arm</th>" not in text and "A Redis cache" not in text


PILOT = os.path.join(ROOT, "runs", "pilot")


@pytest.mark.skipif(not os.path.isdir(PILOT), reason="runs/pilot is not present (run logs are git-ignored)")
def test_every_pilot_request_shows_its_own_text_and_its_own_three_arms(app, monkeypatch):
    """The dropdown label, the request text and all three arms' data must come from one request id."""
    at, _ = app
    monkeypatch.setenv("NL2INFRA_RUNS_DIR", os.path.join(ROOT, "runs"))
    with open(os.path.join(ROOT, "benchmarks", "correctness.jsonl")) as f:
        prompts = {r["id"]: r["prompt"] for r in map(json.loads, f) if True}
    requests = ui.benchmark_requests(PILOT)
    assert len(requests) == 5
    at.run()
    at.selectbox(key="arms_folder").set_value("pilot").run()
    for label, paths in requests.items():
        request_id = label.split(" (repeat")[0]
        logs = {arm: ui.load_log(path) for arm, path in paths.items()}
        # the files on disk: three arms, one id, one prompt, and it is the benchmark's prompt for that id
        assert set(logs) == {"A", "B", "C"}
        assert {log["benchmark"]["id"] for log in logs.values()} == {request_id}
        assert {log["request_id"] for log in logs.values()} == {f"{request_id}-{arm}-r1" for arm in "ABC"}
        assert {log["prompt"] for log in logs.values()} == {prompts[request_id]}
        assert ui.arm_consistency(label, logs) == (request_id, [])

        # the page: after choosing this label, it shows this id, this prompt and these arms' numbers
        at.selectbox(key="arms_request").set_value(label).run()
        assert not at.exception
        assert at.selectbox(key="arms_request").value == label
        text = page_text(at)
        assert f"<b>REQUEST {request_id}</b>" in text
        assert html_escape(prompts[request_id]) in text
        for other_id, other_prompt in prompts.items():
            if other_id != request_id:
                assert html_escape(other_prompt) not in text
        for arm, log in logs.items():
            summary = log["summary"]
            tokens = f"{summary['tokens_in'] + summary['tokens_out']:,}"
            assert (f'<td class="m">{summary["final"]["safety_violations"]}</td><td class="m">{summary["final"]["conformance_violations"]}</td>'
                    f'<td class="m">{summary["fix_rounds"]}</td><td class="m">{tokens}</td>') in text, (label, arm)


def html_escape(text):
    import html
    return html.escape(text).replace("$", "&#36;")
