"""/alerts/webhook is machine-to-machine: Alertmanager proves itself with a shared bearer
token (a compose secret), not a session. No token configured means the route stays locked."""
import pytest
from fastapi.testclient import TestClient

import notifications

ALERT = {"alerts": [{
    "status": "firing",
    "labels": {"alertname": "NL2InfraTestAlert", "severity": "warning"},
    "annotations": {"summary": "test alert"},
}]}


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "alert_webhook_token"
    path.write_text("s3cret-test-token\n", encoding="utf-8")
    monkeypatch.setenv("NL2INFRA_ALERT_WEBHOOK_TOKEN_FILE", str(path))
    return path


def client():
    import server
    return TestClient(server.app)


def test_correct_token_is_accepted_and_emits_an_event(token_file):
    res = client().post("/alerts/webhook", json=ALERT, headers={"Authorization": "Bearer s3cret-test-token"})
    assert res.status_code == 200 and res.json()["emitted"] == 1
    assert any("[NL2InfraTestAlert]" in e["message"] for e in notifications.list_events())


@pytest.mark.parametrize("headers", [
    {},                                                  # missing
    {"Authorization": "Bearer wrong-token"},             # wrong
    {"Authorization": "Bearer s3cret-test-token-extra"}, # prefix of the right one is not enough
    {"Authorization": "Basic s3cret-test-token"},        # wrong scheme
    {"Authorization": "Bearer "},                        # empty
])
def test_missing_or_wrong_token_is_rejected(token_file, headers):
    res = client().post("/alerts/webhook", json=ALERT, headers=headers)
    assert res.status_code == 401
    assert notifications.list_events() == []


def test_no_token_configured_stays_locked(tmp_path, monkeypatch, team_client):
    monkeypatch.setenv("NL2INFRA_ALERT_WEBHOOK_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    assert client().post("/alerts/webhook", json=ALERT, headers={"Authorization": "Bearer "}).status_code == 401
    # Not even a signed-in admin with a CSRF token gets in: the route uses the token only.
    admin = team_client("admin")
    assert admin.post("/alerts/webhook", json=ALERT).status_code == 401
    assert notifications.list_events() == []


def test_empty_token_file_stays_locked(tmp_path, monkeypatch):
    path = tmp_path / "alert_webhook_token"
    path.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("NL2INFRA_ALERT_WEBHOOK_TOKEN_FILE", str(path))
    assert client().post("/alerts/webhook", json=ALERT, headers={"Authorization": "Bearer "}).status_code == 401


def test_token_is_checked_before_the_body_is_read(token_file):
    res = client().post("/alerts/webhook", content=b"not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 401


def test_other_routes_keep_session_auth(token_file):
    # The webhook token is not a general credential.
    headers = {"Authorization": "Bearer s3cret-test-token"}
    assert client().get("/events", headers=headers).status_code == 401
    assert client().get("/api/runs", headers=headers).status_code == 401
