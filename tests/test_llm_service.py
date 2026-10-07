"""LLM service tests with a scripted fake model. No network calls."""
import inspect
import json

import pytest

import llm_service
from contracts import Plan, Resource, Violation
from llm_service import (
    LLMService, LLMBadOutput, LLMUnavailable, _extract_json_block, _extract_yaml,
    image_problem, plan_filenames, plan_problems,
)

PLAN_JSON = json.dumps({"resources": [
    {"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:1.27-alpine", "replicas": 2}},
    {"type": "Service", "name": "web", "namespace": "dev", "spec": {"port": 80}},
]})
PLAN = Plan.model_validate_json(PLAN_JSON)


class Reply:
    def __init__(self, content, tokens_in=11, tokens_out=7):
        self.content = content
        self.usage_metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out}


class HttpError(Exception):
    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.response = type("Response", (), {"status_code": status, "headers": headers or {}})()


class FakeLLM:
    """Only has invoke(): the service cannot bind tools or ask for structured output."""
    def __init__(self, *script):
        self.script = list(script)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Reply) else Reply(item)


def service(*script):
    sleeps = []
    svc = LLMService(llm=FakeLLM(*script), model_name="fake-model", sleep=sleeps.append)
    svc.sleeps = sleeps
    return svc


# ---------------------------------------------------------------- helpers

def test_extract_json_block_raw_and_fenced():
    assert "resources" in _extract_json_block(PLAN_JSON)
    assert _extract_json_block(f"Here is the plan:\n```json\n{PLAN_JSON}\n```\nHope this helps!")["resources"][0]["name"] == "web"
    assert _extract_json_block("no json here") is None


def test_extract_yaml():
    assert _extract_yaml("kind: Service\n") == "kind: Service\n"
    assert _extract_yaml("Here you go:\n```yaml\nkind: Service\n```\nDone.") == "kind: Service\n"
    with pytest.raises(LLMBadOutput):
        _extract_yaml("   \n")


def test_image_problem():
    assert image_problem("nginx:1.27-alpine") is None
    assert image_problem("registry.local:5000/team/app:1.2") is None
    assert image_problem("redis@sha256:abc") is None
    assert "no tag" in image_problem("nginx")
    assert "no tag" in image_problem("registry.local:5000/nginx")
    assert "latest" in image_problem("nginx:latest")


def test_the_model_has_no_tools():
    source = inspect.getsource(llm_service)
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith(("#", '"""')))
    for forbidden in ("with_structured_output(", "bind_tools(", "tool_choice"):
        assert forbidden not in code


def test_client_settings(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    svc = LLMService(model_name="some-model")
    assert svc.temperature == 0.1
    assert svc.llm.temperature == 0.1
    assert svc.llm.request_timeout == 30
    assert svc.llm.max_retries == 0


# ---------------------------------------------------------------- plan

def test_plan_valid():
    svc = service(PLAN_JSON)
    assert svc.plan("deploy nginx", "junior_dev") == PLAN
    assert "deploy nginx" in svc.llm.prompts[0] and "junior_dev" in svc.llm.prompts[0]


def test_plan_braces_in_request_do_not_break_the_prompt():
    svc = service(PLAN_JSON)
    svc.plan('deploy {nginx} with env {"A": "b"}', "junior_dev")
    assert '{"A": "b"}' in svc.llm.prompts[0]


def test_plan_retries_once_with_the_error_in_the_prompt():
    svc = service("Sure! I would deploy nginx.", PLAN_JSON)
    assert svc.plan("deploy nginx", "junior_dev") == PLAN
    assert len(svc.llm.prompts) == 2
    assert "previous reply was rejected" in svc.llm.prompts[1]


@pytest.mark.parametrize("bad", [
    "I cannot do that.",
    '{"resources": []}',                                                                   # empty plan
    '{"resources": [{"type": "Service", "name": "", "namespace": "dev", "spec": {}}]}',    # empty name
    '{"resources": [{"type": "Deployment", "name": "web", "namespace": "dev", "spec": {}}]}',  # no image
    '{"resources": [{"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx:latest"}}]}',
    '{"resources": [{"type": "Deployment", "name": "web", "namespace": "dev", "spec": {"image": "nginx"}}]}',
    '{"resources": [{"type": "Deploy',                                                     # truncated
])
def test_plan_rejected_twice_is_bad_output(bad):
    svc = service(bad, bad)
    with pytest.raises(LLMBadOutput):
        svc.plan("deploy nginx", "junior_dev")
    assert len(svc.llm.prompts) == 2


def test_plan_problems_flags_duplicates():
    res = Resource(type="Service", name="web", namespace="dev", spec={})
    assert plan_problems(Plan(resources=[res, res])) == ["Service/web is listed twice"]


# ---------------------------------------------------------------- generate

def test_generate_makes_one_call_per_resource():
    svc = service("```yaml\nkind: Deployment\n```", "kind: Service")
    files = svc.generate(PLAN, "senior_dev")
    assert files == {"deployment-web.yaml": "kind: Deployment\n", "service-web.yaml": "kind: Service\n"}
    assert len(svc.llm.prompts) == 2
    assert '"type": "Deployment"' in svc.llm.prompts[0] and '"type": "Service"' not in svc.llm.prompts[0]
    assert [c["file"] for c in svc.call_log] == ["deployment-web.yaml", "service-web.yaml"]


def test_generate_prompt_carries_the_same_rule_list_as_arm_b():
    """C = B plus the Gauntlet and fix loop: both prompts must show the model exactly the same rules."""
    from rules import rules_for_role
    for role in ("junior_dev", "senior_dev", "platform_admin"):
        svc = service("kind: Deployment", "kind: Service")
        svc.generate(PLAN, role)
        rules = rules_for_role(role)
        assert "- CKV_K8S_23: Minimize the admission of root containers" in rules and "- OPA_NO_ROOT" in rules
        assert all(rules in prompt for prompt in svc.llm.prompts)
    assert "OPA_READONLY_ROOTFS" not in svc.llm.prompts[0].replace(rules_for_role("platform_admin"), "")
    junior = service("kind: Deployment", "kind: Service")
    junior.generate(PLAN, "junior_dev")
    assert "OPA_REQUIRE_READINESS_PROBE" not in junior.llm.prompts[0]
    assert "CKV_K8S_43" not in junior.llm.prompts[0] and "CKV2_K8S_6" not in junior.llm.prompts[0]


def test_generate_empty_reply_is_bad_output():
    svc = service("kind: Deployment", "")
    with pytest.raises(LLMBadOutput):
        svc.generate(PLAN, "junior_dev")


def test_same_kind_and_name_in_two_namespaces_is_rejected_at_plan_time():
    plan = Plan(resources=[
        Resource(type="Service", name="web", namespace="dev", spec={}),
        Resource(type="Service", name="web", namespace="staging", spec={}),
    ])
    assert plan_problems(plan) == ["Service/web is listed twice"]
    assert plan_filenames(PLAN) == {"Deployment/web": "deployment-web.yaml", "Service/web": "service-web.yaml"}


# ---------------------------------------------------------------- fix

FILES = {"deployment-web.yaml": "kind: Deployment\n# draft 1\n", "service-web.yaml": "kind: Service\n"}


def violation(rule="CKV_K8S_23", file="deployment-web.yaml", line=12, **extra):
    return Violation(tool="checkov", rule_id=rule, severity="MEDIUM", file=file, line=line,
                     message="Minimize the admission of root containers", **extra)


def test_fix_sends_only_the_failing_file_with_rule_line_and_message():
    svc = service("kind: Deployment\n# fixed\n")
    updated = svc.fix(FILES, [violation()], plan=PLAN, round_number=1)
    assert len(svc.llm.prompts) == 1
    prompt = svc.llm.prompts[0]
    assert "CKV_K8S_23" in prompt and "line 12" in prompt and "Minimize the admission of root containers" in prompt
    assert "# draft 1" in prompt and "kind: Service" not in prompt
    assert '"image": "nginx:1.27-alpine"' in prompt
    assert "previous round" not in prompt
    assert updated == {"deployment-web.yaml": "kind: Deployment\n# fixed\n", "service-web.yaml": "kind: Service\n"}
    assert FILES["deployment-web.yaml"].endswith("# draft 1\n")  # input not mutated
    assert svc.call_log[0]["round"] == 1


def test_fix_includes_the_failed_attempt_when_a_rule_fails_twice():
    previous = {"deployment-web.yaml": "kind: Deployment\n# draft 0\n", "service-web.yaml": "kind: Service\n"}
    svc = service("kind: Deployment\n# fixed\n")
    svc.fix(FILES, [violation(), violation(rule="CKV_K8S_8")], plan=PLAN,
            previous_files=previous, previous_violations=[violation()], round_number=2)
    prompt = svc.llm.prompts[0]
    assert "also failed in the previous round: CKV_K8S_23." in prompt
    assert "Your last fix did not work" in prompt
    assert "# draft 0" in prompt and "# draft 1" in prompt
    assert svc.call_log[0]["repeated_rules"] == ["CKV_K8S_23"]


def test_fix_does_not_include_an_attempt_for_a_new_rule():
    previous = {"deployment-web.yaml": "kind: Deployment\n# draft 0\n", "service-web.yaml": "kind: Service\n"}
    svc = service("kind: Deployment\n")
    svc.fix(FILES, [violation(rule="CKV_K8S_8")], plan=PLAN,
            previous_files=previous, previous_violations=[violation()], round_number=2)
    assert "# draft 0" not in svc.llm.prompts[0]


def test_fix_missing_resource_goes_to_its_own_file():
    missing = Violation(tool="plan", rule_id="PLAN_CONFORMANCE_MISSING", severity="HIGH",
                        message="Planned resource Service/web is missing", resource="Service/web")
    svc = service("kind: Service\nmetadata:\n  name: web\n")
    updated = svc.fix({"deployment-web.yaml": "kind: Deployment\n"}, [missing], plan=PLAN)
    assert len(svc.llm.prompts) == 1
    assert "File: service-web.yaml" in svc.llm.prompts[0]
    assert set(updated) == {"deployment-web.yaml", "service-web.yaml"}


def test_fix_empty_reply_is_bad_output():
    svc = service("```yaml\n```")
    with pytest.raises(LLMBadOutput):
        svc.fix(FILES, [violation()], plan=PLAN)


# ---------------------------------------------------------------- transport

def test_tokens_are_recorded_per_call():
    svc = service(Reply(PLAN_JSON, tokens_in=120, tokens_out=45))
    svc.plan("deploy nginx", "junior_dev")
    calls = svc.drain_calls()
    assert len(calls) == 1
    assert (calls[0]["stage"], calls[0]["tokens_in"], calls[0]["tokens_out"], calls[0]["ok"]) == ("plan", 120, 45, True)
    assert calls[0]["model"] == "fake-model"
    assert svc.drain_calls() == []


def test_missing_usage_is_recorded_as_unknown_not_zero():
    reply = Reply("kind: Service")
    reply.usage_metadata = None
    svc = service(reply)
    svc.generate(Plan(resources=[PLAN.resources[1]]), "junior_dev")
    assert svc.call_log[0]["tokens_in"] is None and svc.call_log[0]["tokens_out"] is None


def test_timeout_is_retried_three_times_with_backoff_then_unavailable():
    svc = service(TimeoutError("30 s"), TimeoutError("30 s"), TimeoutError("30 s"), TimeoutError("30 s"))
    with pytest.raises(LLMUnavailable):
        svc.plan("deploy nginx", "junior_dev")
    assert svc.sleeps == [2, 4, 8]
    assert len(svc.call_log) == 4 and not any(c["ok"] for c in svc.call_log)


def test_server_error_then_success():
    svc = service(HttpError(503), PLAN_JSON)
    assert svc.plan("deploy nginx", "junior_dev") == PLAN
    assert svc.sleeps == [2]
    assert [c["ok"] for c in svc.call_log] == [False, True]
    assert svc.call_log[0]["http_status"] == 503


def test_rate_limit_waits_for_retry_after():
    svc = service(HttpError(429, {"retry-after": "7"}), HttpError(429), PLAN_JSON)
    assert svc.plan("deploy nginx", "junior_dev") == PLAN
    assert svc.sleeps == [7.0, 15.0]
    assert svc.call_log[0]["waited_seconds"] == 7.0


def test_rate_limit_gives_up_eventually():
    svc = service(*[HttpError(429, {"retry-after": "1"}) for _ in range(7)])
    with pytest.raises(LLMUnavailable):
        svc.plan("deploy nginx", "junior_dev")
    assert len(svc.sleeps) == 6


def test_client_error_is_not_retried():
    svc = service(HttpError(400))
    with pytest.raises(LLMUnavailable):
        svc.plan("deploy nginx", "junior_dev")
    assert svc.sleeps == [] and len(svc.call_log) == 1
