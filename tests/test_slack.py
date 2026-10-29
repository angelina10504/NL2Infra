"""Slack approval signatures, delivery safety, idempotency and GitHub outcomes."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import notifications
from notifications import slack


def _signed(body: bytes, secret="test-secret", timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return timestamp, signature


def _interaction(action_id="approve_merge", request_id="run-123", actor="demo.user"):
    return {
        "type": "block_actions",
        "user": {"id": "U123", "username": actor},
        "actions": [{"action_id": action_id, "value": request_id}],
        "response_url": "https://hooks.slack.com/actions/test",
    }


def _write_run(tmp_path, **extra):
    run = {
        "request_id": "run-123",
        "status": "passed",
        "user_prompt": "Deploy a small nginx service to staging.",
        "user_role": "developer",
        "approved_plan": {"resources": [{"type": "Deployment", "name": "web", "namespace": "staging"}]},
        "pr_url": "https://github.com/acme/manifests/pull/42",
        "iterations": 1,
        "rounds": [{"files": {"deployment.yaml": "kind: Deployment"}, "violations": []}],
    }
    run.update(extra)
    path = tmp_path / "run-123.json"
    path.write_text(json.dumps(run), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", ["valid", "wrong_signature", "old_timestamp", "missing_timestamp", "missing_signature"])
def test_slack_interaction_signature_verification(monkeypatch, case):
    import server

    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")
    client = TestClient(server.app)
    body = urlencode({"payload": json.dumps({"type": "block_actions", "actions": []})}).encode()
    timestamp = str(int(time.time()) - (600 if case == "old_timestamp" else 0))
    signed_timestamp, signature = _signed(body, timestamp=timestamp)
    headers = {"X-Slack-Request-Timestamp": signed_timestamp, "X-Slack-Signature": signature}
    if case == "wrong_signature":
        headers["X-Slack-Signature"] = "v0=" + "0" * 64
    elif case == "missing_timestamp":
        headers.pop("X-Slack-Request-Timestamp")
    elif case == "missing_signature":
        headers.pop("X-Slack-Signature")
    response = client.post("/slack/interactions", content=body, headers=headers)
    assert response.status_code == (200 if case == "valid" else 401)


def test_verify_signature_accepts_correct_and_rejects_wrong_signature():
    timestamp = str(int(time.time()))
    body = b"payload=ok"
    timestamp, signature = _signed(body, timestamp=timestamp)
    assert slack.verify_signature("test-secret", timestamp, body, signature)
    assert not slack.verify_signature("test-secret", timestamp, body, "v0=" + "0" * 64)
    assert not slack.verify_signature("test-secret", "", body, signature)


def test_approval_merges_once_and_updates_run_and_dashboard(tmp_path, monkeypatch):
    path = _write_run(tmp_path)
    calls = []

    class FakeGitOps:
        def merge_pull_request(self, url):
            calls.append(("merge", url))

        def close_pull_request(self, url):
            calls.append(("close", url))

    updates = []
    monkeypatch.setattr(slack, "_gitops_service", FakeGitOps)
    monkeypatch.setattr(slack, "_update_original", lambda *args: updates.append(args))
    payload = _interaction()
    slack.process_interaction(payload, str(tmp_path))
    slack.process_interaction(payload, str(tmp_path))

    run = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [("merge", "https://github.com/acme/manifests/pull/42")]
    assert run["slack_approval"]["outcome"] == "approved"
    assert run["slack_approval"]["actor"] == "demo.user"
    assert any(event["type"] == "pr_merged" for event in notifications.list_events())
    assert len(updates) == 2


def test_decline_closes_pr_and_records_reason(tmp_path, monkeypatch):
    path = _write_run(tmp_path)
    calls = []

    class FakeGitOps:
        def merge_pull_request(self, url):
            calls.append(("merge", url))

        def close_pull_request(self, url):
            calls.append(("close", url))

    monkeypatch.setattr(slack, "_gitops_service", FakeGitOps)
    monkeypatch.setattr(slack, "_update_original", lambda *args: None)
    slack.process_interaction(_interaction("decline_pr"), str(tmp_path))

    run = json.loads(path.read_text(encoding="utf-8"))
    assert calls == [("close", "https://github.com/acme/manifests/pull/42")]
    assert run["slack_approval"]["outcome"] == "declined"
    assert run["slack_approval"]["reason"] == "Declined via Slack by demo.user"
    assert any(event["type"] == "pr_declined" for event in notifications.list_events())


def test_second_different_click_does_not_repeat_a_completed_action(tmp_path, monkeypatch):
    path = _write_run(tmp_path)
    calls = []

    class FakeGitOps:
        def merge_pull_request(self, url):
            calls.append("merge")

        def close_pull_request(self, url):
            calls.append("close")

    monkeypatch.setattr(slack, "_gitops_service", FakeGitOps)
    monkeypatch.setattr(slack, "_update_original", lambda *args: None)
    slack.process_interaction(_interaction("approve_merge"), str(tmp_path))
    slack.process_interaction(_interaction("decline_pr"), str(tmp_path))
    assert calls == ["merge"]
    assert json.loads(path.read_text(encoding="utf-8"))["slack_approval"]["outcome"] == "approved"


def test_gitops_service_merges_and_closes_only_in_its_configured_repo(monkeypatch):
    from types import SimpleNamespace
    from gitops import GitOpsService

    calls = []

    class PullRequest:
        def merge(self):
            calls.append("merge")
            return SimpleNamespace(merged=True)

        def edit(self, **kwargs):
            calls.append(kwargs)

    class Repo:
        def get_pull(self, number):
            calls.append(number)
            return PullRequest()

    class Client:
        def __init__(self, token):
            assert token == "github-test-token"

        def get_repo(self, name):
            assert name == "acme/manifests"
            return Repo()

    monkeypatch.setattr("github.Github", Client)
    service = GitOpsService(token="github-test-token", repo_name="acme/manifests")
    service.merge_pull_request("https://github.com/acme/manifests/pull/42")
    service.close_pull_request("https://github.com/acme/manifests/pull/42")
    assert calls == [42, "merge", 42, {"state": "closed"}]
    with pytest.raises(ValueError):
        service.merge_pull_request("https://github.com/elsewhere/repo/pull/42")


def test_pr_message_contains_requested_review_details():
    payload = slack.build_approval_message(
        {"run_id": "run-123", "metadata": {"pr_url": "https://github.com/acme/manifests/pull/42"}},
        {
            "request_id": "run-123", "prompt": "Deploy nginx to staging", "role": "developer",
            "plan": {"resources": [{"namespace": "staging"}]}, "iterations": 1,
            "rounds": [{"files": {"deployment.yaml": "..."}, "violations": [{"rule_id": "x"}]}],
            "pr_url": "https://github.com/acme/manifests/pull/42",
        },
        "https://infra.example",
    )
    encoded = json.dumps(payload)
    assert "Deploy nginx to staging" in encoded and "staging" in encoded
    assert "Checkov" in encoded and "plan conformance" in encoded
    assert "deployment.yaml" in encoded and "pull/42" in encoded
    buttons = payload["blocks"][-1]["elements"]
    assert [(b["text"]["text"], b["style"], b["value"]) for b in buttons] == [
        ("Approve and merge", "primary", "run-123"), ("Decline", "danger", "run-123")]


def test_pr_opened_event_posts_block_kit_after_run_log_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://infra.example")
    monkeypatch.setenv("NL2INFRA_RUNS_DIR", str(tmp_path))
    posted = []
    completed = __import__("threading").Event()

    def fake_post(url, payload, timeout=3):
        posted.append((url, payload))
        completed.set()

    monkeypatch.setattr(slack, "_post_json", fake_post)
    notifications.emit_event("pr_opened", "success", "run-123", "Pull request opened",
                             metadata={"pr_url": "https://github.com/acme/manifests/pull/42"})
    _write_run(tmp_path)

    assert completed.wait(2)
    assert posted[0][0] == "https://hooks.slack.com/services/test"
    assert posted[0][1]["blocks"][-1]["elements"][0]["action_id"] == "approve_merge"


def test_pipeline_completes_when_slack_webhook_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    from contracts import Plan, Resource
    from pipeline import Pipeline

    plan = Plan(resources=[Resource(type="Deployment", name="web", namespace="staging",
                                     spec={"image": "nginx:1.27", "replicas": 1})])

    class FakeLLM:
        model_name = "fake-model"
        temperature = 0

        def drain_calls(self):
            return []

        def plan(self, prompt, role):
            return plan

        def generate(self, plan, role):
            return {"deployment.yaml": "kind: Deployment"}

    class FakeValidator:
        last_timings = {}
        last_scan = {}

        def tool_versions(self):
            return {}

        def skipped_checks(self):
            return []

        def validate(self, files, approved_plan, role="developer"):
            return []

    class FakeGitOps:
        def commit_and_pr(self, files, request_id, environment="dev"):
            return "https://github.com/acme/manifests/pull/1"

    state = Pipeline(FakeLLM(), FakeValidator(), FakeGitOps(), auto_approve=True,
                     run_dir=str(tmp_path)).start("Deploy nginx to staging", "developer", request_id="unset-slack")
    assert state.status == "passed" and state.pr_url.endswith("/pull/1")
