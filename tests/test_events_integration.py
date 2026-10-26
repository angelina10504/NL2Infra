"""End-to-end: the pipeline emits the right notification events from the graph nodes.

Uses scripted fake services (no LLM, no scanners). The autouse `_isolate_events` fixture
gives each test its own events store.
"""
import notifications
from contracts import Plan, Resource, Violation
from pipeline import Pipeline

PLAN = Plan(resources=[
    Resource(type="Deployment", name="web", namespace="staging", spec={"image": "nginx:1.27-alpine", "replicas": 2}),
])


class FakeLLM:
    model_name = "fake-model"
    temperature = 0.1

    def drain_calls(self):
        return []

    def plan(self, prompt, role):
        return PLAN

    def generate(self, plan, role):
        return {"deployment-web.yaml": "draft 0"}

    def fix(self, files, violations, plan, previous_files=None, previous_violations=None, round_number=0):
        return {**files, "deployment-web.yaml": f"draft {round_number}"}


class FakeValidator:
    def __init__(self, *script):
        self.script = list(script) or [[]]
        self.last_timings = {"checkov": 0.1}
        self.last_scan = {}

    def tool_versions(self):
        return {"checkov": "3", "conftest": "dev", "opa": "1", "kubectl": "v1"}

    def skipped_checks(self):
        return []

    def validate(self, files, plan, role="developer"):
        return list(self.script[0] if len(self.script) == 1 else self.script.pop(0))


class FakeGitOps:
    def commit_and_pr(self, files, request_id, environment="dev"):
        return "https://github.com/acme/nl2infra-manifests/pull/7"


def _types():
    return [e["type"] for e in notifications.list_events()]


def _pipeline(tmp_path, validator=None, auto_approve=True):
    return Pipeline(FakeLLM(), validator or FakeValidator(), FakeGitOps(),
                    auto_approve=auto_approve, run_dir=str(tmp_path))


def test_blocked_request_emits_request_blocked(tmp_path):
    _pipeline(tmp_path).start("Write a poem about coffee", "developer", request_id="r1")
    assert "request_blocked" in _types()


def test_approval_required_emitted_when_run_pauses(tmp_path):
    _pipeline(tmp_path, auto_approve=False).start("Deploy nginx to staging", "developer", request_id="r2")
    assert "approval_required" in _types()


def test_pr_opened_emitted_on_success(tmp_path):
    _pipeline(tmp_path, validator=FakeValidator([])).start("Deploy nginx to staging", "developer", request_id="r3")
    types = _types()
    assert "pr_opened" in types
    event = next(e for e in notifications.list_events() if e["type"] == "pr_opened")
    assert event["severity"] == "success" and event["metadata"]["environment"] == "staging"


def test_fix_loop_exhausted_emitted(tmp_path):
    bad = Violation(tool="checkov", rule_id="CKV_K8S_23", severity="MEDIUM",
                    file="deployment-web.yaml", line=3, message="bad")
    _pipeline(tmp_path, validator=FakeValidator([bad])).start("Deploy nginx to staging", "developer", request_id="r4")
    assert "fix_loop_exhausted" in _types()
