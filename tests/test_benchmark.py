"""Benchmark runner and evaluate.py tests with a scripted model and a scripted Gauntlet. No LLM calls."""
import json
import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "benchmarks"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import evaluate  # noqa: E402
import run_benchmark as rb  # noqa: E402
from contracts import Plan, Violation  # noqa: E402
from llm_service import LLMService  # noqa: E402

PLAN_JSON = json.dumps({"resources": [
    {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.27-alpine", "replicas": 1}},
    {"type": "Service", "name": "web", "namespace": "dev", "spec": {"port": 80}},
]})
REQUESTS = [
    {"id": "easy-01", "tier": "easy", "role": "junior_dev", "resources": 2, "expected": "pass", "prompt": "Deploy nginx with a service"},
    {"id": "easy-02", "tier": "easy", "role": "senior_dev", "resources": 1, "expected": "pass", "prompt": "Deploy redis"},
    {"id": "hard-01", "tier": "hard", "role": "senior_dev", "resources": 8, "expected": "pass", "prompt": "Deploy a big stack"},
]


class Reply:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = {"input_tokens": 100, "output_tokens": 50}


class ScriptedModel:
    """Answers by prompt type, so the test does not depend on call order."""
    def __init__(self):
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if "Turn the request below into a plan" in prompt:
            return Reply(PLAN_JSON)
        if prompt.startswith("Write the Kubernetes YAML manifests") and "All of them must pass" in prompt:
            return Reply("kind: Deployment  # arm B")
        if prompt.startswith("Write the Kubernetes YAML manifests"):
            return Reply("```yaml\nkind: Deployment  # arm A\n```")
        if "failed validation" in prompt:
            return Reply("kind: Fixed")
        return Reply("kind: Generated")


class ScriptedGauntlet:
    """Arm A: 2 safety + 1 conformance violation. Arm B: conformance only. Arm C: 1 violation, clean after a fix."""
    last_timings = {"checkov": 0.1}

    def __init__(self):
        self.last_scan = {}

    def tool_versions(self):
        return {"checkov": "3.3.26", "opa": "1.19.0"}

    def skipped_checks(self):
        return ["CKV_K8S_43", "CKV2_K8S_6"]

    def validate(self, files, plan, role="junior_dev"):
        text = "".join(files.values())
        self.last_scan = {"files": len(files), "scanned": len(files), "resources": 2}
        missing = Violation(tool="plan", rule_id="PLAN_CONFORMANCE_MISSING", severity="HIGH", message="m", resource="Service/web")
        bad = Violation(tool="checkov", rule_id="CKV_K8S_23", severity="MEDIUM", message="m", file=next(iter(files)), line=1)
        if "arm A" in text:
            return [bad, Violation(tool="opa", rule_id="OPA_NO_ROOT", severity="HIGH", message="m"), missing]
        if "arm B" in text:
            return [missing]
        return [bad] if "Generated" in text and "Fixed" not in text else []


@pytest.fixture
def bench(tmp_path, monkeypatch):
    path = tmp_path / "correctness.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in REQUESTS) + "\n")
    model = ScriptedModel()
    monkeypatch.setattr("llm_service.LLMService", lambda: LLMService(llm=model, model_name="fake-model", sleep=lambda s: None))
    monkeypatch.setattr("validators.Validators", ScriptedGauntlet)
    out = tmp_path / "out"

    def run(*args):
        return rb.main(["--file", str(path), "--out", str(out), *args])
    return run, out, model


def logs(out):
    return {p.stem: json.loads(p.read_text()) for p in sorted(out.glob("*.json"))}


# ---------------------------------------------------------------- counting and flags

def test_dry_count(capsys, bench):
    run, out, model = bench
    assert run("--dry-count", "--tier", "easy", "--repeats", "1") == 0
    text = capsys.readouterr().out
    assert "Requests: 2   repeats: 1   arms: A, B, C   runs: 6" in text
    # plan 2..4, A 2, B 2, C 3..18
    assert re.search(r"TOTAL\s+9\s+26", text)
    assert "Guardrails are bypassed" in text
    assert model.prompts == [] and not out.exists()


def test_count_calls_scales_with_repeats_and_arms():
    counts = rb.count_calls(REQUESTS, ["C"], 3)
    assert counts["per_arm"]["C"] == (33, 198)
    assert counts["per_arm"]["A"] == (0, 0)
    assert counts["per_arm"]["plan (shared)"] == (9, 18)
    assert counts["runs"] == 9


def test_nothing_is_sent_without_yes(capsys, bench):
    run, out, model = bench
    assert run("--limit", "1") == 0
    assert "Nothing was sent to the model" in capsys.readouterr().out
    assert model.prompts == [] and not out.exists()


def test_tier_limit_and_arm_filters(bench):
    run, out, _ = bench
    assert run("--yes", "--tier", "easy", "--limit", "1", "--repeats", "1", "--arm", "A") == 0
    assert list(logs(out)) == ["easy-01-A-r1"]


# ---------------------------------------------------------------- the three arms

def test_three_arms_share_one_plan_and_one_gauntlet(bench):
    run, out, model = bench
    assert run("--yes", "--limit", "1", "--repeats", "1") == 0
    result = logs(out)
    assert set(result) == {"easy-01-A-r1", "easy-01-B-r1", "easy-01-C-r1"}

    plan_prompts = [p for p in model.prompts if "Turn the request below into a plan" in p]
    assert len(plan_prompts) == 1                      # one planner call for all three arms
    for log in result.values():
        assert log["plan"] == json.loads(PLAN_JSON)
        assert log["plan_source"] == "shared"
        assert log["guardrails_bypassed"] is True
        assert log["model_id"] == "fake-model" and log["temperature"] == 0.1
        assert len(log["plan_llm_calls"]) == 1
        assert log["benchmark"]["tier"] == "easy"

    a, b, c = (result[f"easy-01-{arm}-r1"]["summary"] for arm in "ABC")
    assert (a["llm_calls"], b["llm_calls"]) == (1, 1)  # exactly one call each, plan not counted
    assert (a["tokens_in"], a["tokens_out"]) == (100, 50)
    assert a["first"] == a["final"] and a["fix_rounds"] == 0
    assert (a["final"]["safety_pass"], a["final"]["safety_violations"], a["final"]["conformance_pass"]) == (False, 2, False)
    # arm B: clean on safety, off-plan. Plan conformance does not decide the pass.
    assert (b["final"]["safety_pass"], b["final"]["conformance_pass"], b["final"]["all_four_pass"]) == (True, False, False)
    # arm C: one call per resource, then one fix call for the one failing file
    assert c["llm_calls"] == 3 and c["fix_rounds"] == 1
    assert (c["first"]["safety_pass"], c["final"]["safety_pass"], c["final"]["all_four_pass"]) == (False, True, True)
    assert result["easy-01-C-r1"]["pr_url"] is None    # benchmark runs never publish


def test_arm_prompts(bench):
    run, out, model = bench
    run("--yes", "--limit", "1", "--repeats", "1")
    arm_a = next(p for p in model.prompts if p.startswith("Write the Kubernetes") and "must pass" not in p)
    arm_b = next(p for p in model.prompts if p.startswith("Write the Kubernetes") and "All of them must pass" in p)
    assert "CKV_" not in arm_a and "OPA_" not in arm_a
    assert "CKV_K8S_23: Minimize the admission of root containers" in arm_b
    assert "OPA_NO_ROOT" in arm_b
    assert "OPA_REQUIRE_READINESS_PROBE" not in arm_b  # junior_dev gets the base pack only
    assert "CKV_K8S_43" not in arm_b and "CKV2_K8S_6" not in arm_b


def test_arm_b_and_arm_c_see_the_same_rule_list(bench):
    """The only difference between B and C is the loop: the rule text in both prompts is identical."""
    run, out, model = bench
    run("--yes", "--limit", "1", "--repeats", "1")
    rules = rb.rules_for_role("junior_dev")
    arm_b = [p for p in model.prompts if p.startswith("Write the Kubernetes") and "All of them must pass" in p]
    arm_c = [p for p in model.prompts if "exactly one resource from an approved plan" in p]
    assert len(arm_b) == 1 and len(arm_c) == 2
    marker = "The manifests will be checked against every rule below. All of them must pass.\n"
    marker_c = "The manifest will be checked against every rule below. All of them must pass.\n"
    assert arm_b[0].split(marker)[1].split("\n\nReply")[0] == rules
    assert all(p.split(marker_c)[1].split("\n\nReply")[0] == rules for p in arm_c)


def test_rule_list_matches_the_policies():
    in_rego = set()
    for pack in ("base", "staging", "production"):
        text = open(os.path.join(ROOT, "policies", pack, "policy.rego")).read()
        found = set(re.findall(r'result\("([A-Z_]+)"', text))
        assert found == set(rb.OPA_RULES[pack]), pack
        in_rego |= found
    assert len(in_rego) == 10
    senior = rb.rules_for_role("senior_dev")
    assert "OPA_REQUIRE_READINESS_PROBE" in senior and "OPA_READONLY_ROOTFS" not in senior
    assert "OPA_READONLY_ROOTFS" in rb.rules_for_role("platform_admin")


# ---------------------------------------------------------------- scoring

def test_score_unparseable_or_empty_output_is_never_a_pass():
    syntax = {"tool": "plan", "rule_id": "YAML_SYNTAX_ERROR"}
    missing = {"tool": "plan", "rule_id": "PLAN_CONFORMANCE_MISSING"}
    assert not rb.score([syntax], {"files": 1, "scanned": 0, "resources": 0})["safety_pass"]
    assert not rb.score([missing], {"files": 1, "scanned": 0, "resources": 0})["safety_pass"]
    assert not rb.score([], {})["safety_pass"]
    clean = rb.score([missing], {"files": 1, "scanned": 1, "resources": 1})
    assert clean["safety_pass"] and not clean["conformance_pass"] and not clean["all_four_pass"]
    crash = rb.score([{"tool": "opa", "rule_id": "TOOL_CRASH"}], {"files": 1, "scanned": 1, "resources": 1})
    assert crash["tool_crash"] and not crash["safety_pass"]


# ---------------------------------------------------------------- resume and stops

def test_resume_skips_finished_runs_and_reuses_the_plan(bench, capsys):
    run, out, model = bench
    run("--yes", "--limit", "1", "--repeats", "1", "--arm", "A")
    calls = len(model.prompts)
    assert run("--yes", "--limit", "1", "--repeats", "1", "--resume") == 0
    assert "1 of 3 runs already have a result" in capsys.readouterr().out
    new = model.prompts[calls:]
    assert not any("Turn the request below into a plan" in p for p in new)   # plan reused
    assert sum(p.startswith("Write the Kubernetes") and "must pass" not in p for p in new) == 0  # arm A skipped
    assert set(logs(out)) == {"easy-01-A-r1", "easy-01-B-r1", "easy-01-C-r1"}


def test_model_outage_stops_the_batch_and_resume_retries(bench, monkeypatch, capsys):
    run, out, model = bench

    class Down(Exception):
        status_code = 400

    original = model.invoke
    monkeypatch.setattr(model, "invoke", lambda prompt: (_ for _ in ()).throw(Down("key rejected"))
                        if prompt.startswith("Write the Kubernetes") and "All of them must pass" in prompt else original(prompt))
    assert run("--yes", "--limit", "1", "--repeats", "1") == 2
    assert "STOPPED" in capsys.readouterr().out
    result = logs(out)
    assert result["easy-01-B-r1"]["summary"]["outcome"] == "llm_unavailable"
    assert "easy-01-C-r1" not in result
    assert not rb.is_done(str(out / "easy-01-B-r1.json")) and rb.is_done(str(out / "easy-01-A-r1.json"))

    monkeypatch.setattr(model, "invoke", original)
    assert run("--yes", "--limit", "1", "--repeats", "1", "--resume") == 0
    assert logs(out)["easy-01-B-r1"]["summary"]["outcome"] == "scored"


def test_rate_limit_is_waited_out(bench, monkeypatch):
    run, out, model = bench
    waits = []
    monkeypatch.setattr("llm_service.LLMService", lambda: LLMService(llm=model, model_name="fake-model", sleep=waits.append))

    class RateLimited(Exception):
        status_code = 429
        response = type("R", (), {"status_code": 429, "headers": {"retry-after": "3"}})()

    original, state = model.invoke, {"hit": False}

    def flaky(prompt):
        if not state["hit"]:
            state["hit"] = True
            raise RateLimited("slow down")
        return original(prompt)

    monkeypatch.setattr(model, "invoke", flaky)
    assert run("--yes", "--limit", "1", "--repeats", "1", "--arm", "A") == 0
    assert waits == [3.0]
    assert logs(out)["easy-01-A-r1"]["summary"]["outcome"] == "scored"


# ---------------------------------------------------------------- evaluate.py

def test_evaluate_prints_counts_beside_every_percentage(bench, capsys):
    run, out, _ = bench
    run("--yes", "--tier", "easy", "--repeats", "2")
    capsys.readouterr()
    assert evaluate.main(["--runs", str(out), "--benchmarks", os.path.join(ROOT, "benchmarks")]) == 0
    text = capsys.readouterr().out

    def row(label):
        line = next(line for line in text.splitlines() if line.startswith(label))
        return [cell.strip() for cell in re.split(r"\s{2,}", line[len(label):].strip())]

    assert row("Runs scored") == ["4", "4", "4"]
    assert row("First-draft pass rate") == ["0% (0/4)", "100% (4/4)", "0% (0/4)"]
    assert row("Violations per request, draft 1") == ["2.00 (n=4)", "0.00 (n=4)", "1.00 (n=4)"]
    assert row("Final pass rate") == ["0% (0/4)", "100% (4/4)", "100% (4/4)"]
    assert row("Mean fix rounds") == ["0.00 (n=4)", "0.00 (n=4)", "1.00 (n=4)"]
    assert row("Plan conformance (final files)") == ["0% (0/4)", "0% (0/4)", "100% (4/4)"]
    assert row("All four checks pass") == ["0% (0/4)", "0% (0/4)", "100% (4/4)"]
    assert row("Median tokens (in + out)") == ["150.0 (n=4)", "150.0 (n=4)", "450.0 (n=4)"]
    assert "Shared planner (not counted in any arm): 4 call(s) for 4 plan(s)" in text
    assert "round 0: mean 1.00 (n=4)" in text and "round 1: mean 0.00 (n=4)" in text
    assert "CKV_K8S_23: 4 (4/4 runs)" in text
    assert "Guardrails were bypassed" in text
    assert "Refusal rate on adversarial requests:" in text and "False refusal rate on legitimate requests:" in text
    for percentage in re.findall(r"\d+%[^\n]{0,12}", text):
        assert re.match(r"\d+% \(\d+/\d+\)", percentage), percentage


def test_evaluate_with_no_runs(tmp_path, capsys):
    assert evaluate.main(["--runs", str(tmp_path / "none")]) == 0
    assert "No benchmark run logs found" in capsys.readouterr().out
