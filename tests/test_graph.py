"""Graph routing tests with scripted services. No LLM, no scanners, no network."""
import json

import pytest

from contracts import Plan, Resource, Violation
from graph import MAX_FIX_ROUNDS, create_graph
from llm_service import LLMBadOutput, LLMUnavailable
from pipeline import Pipeline

PLAN = Plan(resources=[
    Resource(type="Deployment", name="web", namespace="staging", spec={"image": "nginx:1.27-alpine", "replicas": 2}),
    Resource(type="Service", name="web", namespace="staging", spec={"port": 80}),
])


def violation(rule="CKV_K8S_23", tool="checkov"):
    return Violation(tool=tool, rule_id=rule, severity="MEDIUM", file="deployment-web.yaml", line=3, message="bad")


CRASH = Violation(tool="opa", rule_id="TOOL_CRASH", severity="CRITICAL", message="rego_parse_error")


class FakeLLM:
    model_name = "fake-model"
    temperature = 0.1

    def __init__(self, fail_in=None, error=None):
        self.fail_in, self.error = fail_in, error
        self.calls = []
        self.fix_args = []
        self.generate_roles = []
        self._log = []

    def _step(self, stage):
        self.calls.append(stage)
        self._log.append({"stage": stage, "tokens_in": 100, "tokens_out": 40, "seconds": 0.5, "ok": True})
        if self.fail_in == stage:
            raise self.error

    def drain_calls(self):
        log, self._log = self._log, []
        return log

    def plan(self, prompt, role):
        self._step("plan")
        return PLAN

    def generate(self, plan, role):
        self.generate_roles.append(role)
        self._step("generate")
        return {"deployment-web.yaml": "draft 0", "service-web.yaml": "svc"}

    def fix(self, files, violations, plan, previous_files=None, previous_violations=None, round_number=0):
        self._step("fix")
        self.fix_args.append({"files": dict(files), "violations": list(violations),
                              "previous_files": previous_files, "previous_violations": previous_violations,
                              "round": round_number})
        return {**files, "deployment-web.yaml": f"draft {round_number}"}


class FakeValidator:
    """Returns the scripted result for each validation; the last entry repeats forever."""
    def __init__(self, *script):
        self.script = list(script) or [[]]
        self.seen = []
        self.last_timings = {"checkov": 0.1}

    def tool_versions(self):
        return {"checkov": "3.3.26", "conftest": "dev", "opa": "1.19.0", "kubectl": "v1.37.0"}

    def skipped_checks(self):
        return ["CKV_K8S_43", "CKV2_K8S_6"]

    def validate(self, files, plan, role="junior_dev"):
        self.seen.append({"files": dict(files), "plan": plan, "role": role})
        result = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return list(result)


class FakeGitOps:
    def __init__(self, result="https://github.com/acme/nl2infra-manifests/pull/7"):
        self.result = result
        self.calls = []

    def commit_and_pr(self, files, request_id, environment="dev"):
        self.calls.append({"files": dict(files), "request_id": request_id, "environment": environment})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make(tmp_path, llm=None, validator=None, gitops=None, auto_approve=True):
    llm, validator, gitops = llm or FakeLLM(), validator or FakeValidator(), gitops or FakeGitOps()
    pipeline = Pipeline(llm, validator, gitops, auto_approve=auto_approve, run_dir=str(tmp_path))
    return pipeline, llm, validator, gitops


def run(tmp_path, prompt="Deploy an nginx server to staging", role="senior_dev", **services):
    pipeline, llm, validator, gitops = make(tmp_path, **services)
    state = pipeline.start(prompt, role, request_id="req1")
    return state, llm, validator, gitops


def saved(tmp_path, request_id="req1"):
    return json.loads((tmp_path / f"{request_id}.json").read_text())


# ---------------------------------------------------------------- order and guardrails

def test_nodes_run_in_the_specified_order(tmp_path):
    pipeline, *_ = make(tmp_path, validator=FakeValidator([violation()], []))
    steps = []
    pipeline.start("Deploy an nginx server to staging", "senior_dev", on_step=lambda name, state: steps.append(name))
    assert steps == ["guardrails", "plan", "approve", "generate", "gauntlet", "fix", "gauntlet", "pull_request", "deploy"]


def test_guardrails_reject_before_any_llm_call(tmp_path):
    state, llm, validator, gitops = run(tmp_path, prompt="Write a poem about sunflowers and coffee")
    assert state.status == "rejected"
    assert state.rejection_reason.startswith("off_topic")
    assert llm.calls == [] and validator.seen == [] and gitops.calls == []
    assert saved(tmp_path)["final_status"] == "rejected"


# ---------------------------------------------------------------- approval

def test_run_pauses_for_approval_then_continues(tmp_path):
    pipeline, llm, validator, gitops = make(tmp_path, auto_approve=False)
    state = pipeline.start("Deploy an nginx server to staging", "senior_dev", request_id="req1")
    assert pipeline.awaiting_approval("req1")
    assert state.status == "running" and state.plan == PLAN and state.approved_plan is None
    assert llm.calls == ["plan"]
    assert not (tmp_path / "req1.json").exists()

    state = pipeline.resume("req1", approved=True)
    assert not pipeline.awaiting_approval("req1")
    assert state.status == "passed"
    assert state.approved_plan == PLAN
    assert llm.calls == ["plan", "generate"]  # the plan is not asked for again
    assert llm.generate_roles == ["senior_dev"]  # the generate prompt gets the role's rule list
    assert validator.seen[0]["plan"] == PLAN and validator.seen[0]["role"] == "senior_dev"
    assert state.pr_url == "https://github.com/acme/nl2infra-manifests/pull/7"
    assert state.deploy == {"status": "awaiting_merge"}
    assert gitops.calls[0]["environment"] == "staging"


def test_declined_plan_is_rejected_and_nothing_is_generated(tmp_path):
    pipeline, llm, validator, gitops = make(tmp_path, auto_approve=False)
    pipeline.start("Deploy an nginx server to staging", "senior_dev", request_id="req1")
    state = pipeline.resume("req1", approved=False)
    assert state.status == "rejected"
    assert state.rejection_reason.startswith("plan_not_approved")
    assert llm.calls == ["plan"] and validator.seen == [] and gitops.calls == []
    assert saved(tmp_path)["final_status"] == "rejected"


def test_resume_without_a_pending_approval_is_an_error(tmp_path):
    pipeline, *_ = make(tmp_path)
    pipeline.start("Deploy an nginx server to staging", "senior_dev", request_id="req1")
    with pytest.raises(ValueError):
        pipeline.resume("req1", approved=True)


def test_graph_without_auto_approve_stops_at_the_approval_step():
    from langgraph.checkpoint.memory import MemorySaver
    llm = FakeLLM()
    graph = create_graph(llm, FakeValidator(), FakeGitOps(), checkpointer=MemorySaver())
    from contracts import RunState
    config = {"configurable": {"thread_id": "t"}}
    graph.invoke(RunState(request_id="t", user_role="senior_dev", user_prompt="Deploy a server", model="m"), config)
    assert graph.get_state(config).next == ("approve",)
    assert llm.calls == ["plan"]


# ---------------------------------------------------------------- fix loop

def test_zero_violations_publishes_without_fixing(tmp_path):
    state, llm, validator, gitops = run(tmp_path)
    assert (state.status, state.iterations) == ("passed", 0)
    assert llm.calls == ["plan", "generate"] and len(gitops.calls) == 1


@pytest.mark.parametrize("rounds_needed", [1, 3, 5])
def test_violations_cleared_after_n_fix_rounds(tmp_path, rounds_needed):
    script = [[violation()]] * rounds_needed + [[]]
    state, llm, validator, gitops = run(tmp_path, validator=FakeValidator(*script))
    assert (state.status, state.iterations) == ("passed", rounds_needed)
    assert llm.calls.count("fix") == rounds_needed
    assert len(validator.seen) == rounds_needed + 1
    assert gitops.calls[0]["files"]["deployment-web.yaml"] == f"draft {rounds_needed}"


def test_escalates_after_exactly_five_fix_rounds(tmp_path):
    state, llm, validator, gitops = run(tmp_path, validator=FakeValidator([violation()]))
    assert MAX_FIX_ROUNDS == 5
    assert state.status == "escalated"
    assert state.rejection_reason.startswith("max_fix_rounds")
    assert state.iterations == 5
    assert llm.calls.count("fix") == 5
    assert len(validator.seen) == 6
    assert gitops.calls == [] and state.pr_url is None
    assert [r["round"] for r in state.rounds] == [0, 1, 2, 3, 4, 5]
    assert [r["files"]["deployment-web.yaml"] for r in state.rounds] == [f"draft {i}" for i in range(6)]
    assert len(state.violations) == 1


def test_fix_receives_the_previous_round_for_repeated_rules(tmp_path):
    script = [[violation()], [violation(), violation("CKV_K8S_8")], []]
    state, llm, *_ = run(tmp_path, validator=FakeValidator(*script))
    first, second = llm.fix_args
    assert first["round"] == 1 and first["previous_files"] is None
    assert second["round"] == 2
    assert second["files"]["deployment-web.yaml"] == "draft 1"
    assert second["previous_files"]["deployment-web.yaml"] == "draft 0"
    assert [v.rule_id for v in second["previous_violations"]] == ["CKV_K8S_23"]
    assert [v.rule_id for v in second["violations"]] == ["CKV_K8S_23", "CKV_K8S_8"]


@pytest.mark.parametrize("script,validations", [
    ([[violation(), CRASH]], 1),
    ([[violation()], [CRASH]], 2),
])
def test_tool_crash_escalates_and_is_never_sent_to_the_fix_loop(tmp_path, script, validations):
    state, llm, validator, gitops = run(tmp_path, validator=FakeValidator(*script))
    assert state.status == "escalated"
    assert state.rejection_reason.startswith("tool_crash: opa: rego_parse_error")
    assert len(validator.seen) == validations
    assert llm.calls.count("fix") == validations - 1
    assert gitops.calls == []


# ---------------------------------------------------------------- errors never crash a run

@pytest.mark.parametrize("stage", ["plan", "generate", "fix"])
@pytest.mark.parametrize("error,reason", [
    (LLMUnavailable("timed out after 30 s"), "llm_unavailable"),
    (LLMBadOutput("the model returned no YAML"), "bad_llm_output"),
])
def test_llm_failure_ends_as_escalated(tmp_path, stage, error, reason):
    llm = FakeLLM(fail_in=stage, error=error)
    state, llm, validator, gitops = run(tmp_path, llm=llm, validator=FakeValidator([violation()]))
    assert state.status == "escalated"
    assert state.rejection_reason.startswith(reason)
    assert llm.calls[-1] == stage
    assert gitops.calls == []
    log = saved(tmp_path)
    assert log["final_status"] == "escalated"
    assert len(log["llm_calls"]) == len(llm.calls)  # the failing call is logged too


def test_unexpected_exception_in_a_node_ends_as_escalated(tmp_path):
    state, *_ = run(tmp_path, validator=FakeValidator(RuntimeError("disk full")))
    assert state.status == "escalated"
    assert state.rejection_reason == "internal_error: RuntimeError: disk full"


def test_failed_push_keeps_the_run_passed_without_a_url(tmp_path):
    state, *_ = run(tmp_path, gitops=FakeGitOps(RuntimeError("403 Forbidden")))
    assert state.status == "passed"
    assert state.pr_url is None
    assert "403 Forbidden" in state.pr_error
    assert state.deploy == {"status": "not_published"}


def test_a_non_http_result_is_never_stored_as_the_pr_url(tmp_path):
    state, *_ = run(tmp_path, gitops=FakeGitOps("file:///tmp/nl2infra-manifests/dev/req1 (Local GitOps simulated)"))
    assert state.status == "passed" and state.pr_url is None
    assert state.pr_error.startswith("file://")


# ---------------------------------------------------------------- run log

def test_run_log_has_every_required_field(tmp_path):
    script = [[violation(), violation("OPA_NO_ROOT", "opa")], [violation()], []]
    state, llm, *_ = run(tmp_path, validator=FakeValidator(*script))
    log = saved(tmp_path)

    assert log["request_id"] == "req1"
    assert log["role"] == "senior_dev"
    assert log["prompt"] == "Deploy an nginx server to staging"
    assert log["model_id"] == "fake-model"
    assert log["temperature"] == 0.1
    assert log["plan"]["resources"][0]["name"] == "web"
    assert log["approved_plan"] == log["plan"]
    assert log["fix_rounds"] == 2
    assert log["final_status"] == "passed"
    assert log["pr_url"].startswith("https://github.com/")
    assert log["date"] and log["date"] == log["started_at"]
    assert "commit" in log
    assert log["tool_versions"]["opa"] == "1.19.0"
    assert log["skipped_checks"] == ["CKV_K8S_43", "CKV2_K8S_6"]

    # every draft and every violation, per round
    assert [r["round"] for r in log["rounds"]] == [0, 1, 2]
    assert [r["files"]["deployment-web.yaml"] for r in log["rounds"]] == ["draft 0", "draft 1", "draft 2"]
    assert [[v["rule_id"] for v in r["violations"]] for r in log["rounds"]] == [["CKV_K8S_23", "OPA_NO_ROOT"], ["CKV_K8S_23"], []]
    assert log["rounds"][0]["violations"][0].keys() == {"tool", "rule_id", "severity", "file", "line", "message", "resource"}
    assert log["rounds"][0]["tool_seconds"] == {"checkov": 0.1}

    # tokens per call, and totals
    assert [c["stage"] for c in log["llm_calls"]] == ["plan", "generate", "fix", "fix"]
    assert all(c["tokens_in"] == 100 and c["tokens_out"] == 40 for c in log["llm_calls"])
    assert (log["metrics"]["tokens_in"], log["metrics"]["tokens_out"], log["metrics"]["calls"]) == (400, 160, 4)
    assert log["metrics"]["llm_time"] == 2.0

    for key in ("guardrails_time", "planning_time", "generation_time", "validation_time", "fix_time", "pr_time", "total_time"):
        assert key in log["stage_timings"]


def test_run_log_round_trips_through_load_runs(tmp_path):
    from storage import load_runs
    run(tmp_path)
    runs = load_runs(str(tmp_path))
    assert len(runs) == 1 and runs[0].request_id == "req1" and runs[0].status == "passed"


def test_shipped_mocks_reach_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    pipeline = Pipeline(auto_approve=True, run_dir=str(tmp_path))
    state = pipeline.start("Deploy a server to the default namespace", "junior_dev")
    assert (state.status, state.iterations, state.model) == ("passed", 1, "mock")
    assert state.pr_url.startswith("https://github.com/mock/")


def test_mock_mode_is_off_unless_asked_for(monkeypatch):
    from graph import default_services
    from llm_service import LLMService
    monkeypatch.delenv("MOCK_MODE", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    llm, _, _ = default_services()
    assert isinstance(llm, LLMService)
