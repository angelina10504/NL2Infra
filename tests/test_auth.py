"""Exercise real session cookies and role checks at the HTTP and Slack boundaries."""
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import auth
import server
from contracts import Plan, Resource, RunState
from notifications import slack


@pytest.fixture(autouse=True)
def isolated_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("NL2INFRA_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(server, "_pipeline", None)
    monkeypatch.setattr(server, "_live_runs", {})


@pytest.mark.parametrize("method,path", [("get", "/api/runs"), ("get", "/events"),
    ("get", "/events/stream"), ("get", "/api/team"), ("post", "/api/run"),
    ("post", "/ask"), ("post", "/api/approve"), ("post", "/api/pr/review"),
    ("post", "/api/team"), ("post", "/api/why")])
def test_anonymous_and_forged_role_cannot_access_project(method, path):
    response = getattr(TestClient(server.app), method)(path, headers={"X-Role": "admin", "X-User": "owner"})
    assert response.status_code == 401


def test_no_credentials_does_not_enable_a_demo_admin(monkeypatch):
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_SECRET", raising=False)
    client = TestClient(server.app)
    assert client.get("/auth/login").status_code == 503
    assert client.get("/api/session").json()["user"] is None
    assert client.get("/api/runs").status_code == 401


def test_local_sign_in_origin_is_independent_of_slack_tunnel(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://slack-tunnel.test")
    monkeypatch.setenv("NL2INFRA_AUTH_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    response = TestClient(server.app).get("/auth/login", follow_redirects=False)
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["redirect_uri"] == ["http://localhost:8000/auth/callback"]
    assert "Secure" not in response.headers["set-cookie"]


def test_session_expiry_logout_and_csrf(team_client):
    client = team_client()
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post("/api/logout").status_code == 403
    client.headers["X-CSRF-Token"] = "forged"
    assert client.post("/api/logout").status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    token = client.cookies.get(auth.SESSION_COOKIE)
    assert client.post("/api/logout").status_code == 200
    assert auth.session(token) is None
    client = team_client()
    with auth.database() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time()-1,))
    assert client.get("/events").status_code == 401


def test_viewer_cannot_create_requests(team_client, monkeypatch):
    def forbidden():
        pytest.fail("The pipeline must not be called")
    monkeypatch.setattr(server, "get_pipeline", forbidden)
    client = team_client("viewer")
    for path in ("/api/run", "/ask"):
        assert client.post(path, json={"prompt": "Deploy nginx", "role": "developer"}).status_code == 403


def fake_pipeline(monkeypatch, tmp_path):
    calls = []
    state = RunState(request_id="draft", user_role="developer", user_prompt="Deploy nginx to dev", model="fake",
                     plan=Plan(resources=[Resource(type="Service", name="web", namespace="dev", spec={})]))

    def start(prompt, role, request_id, on_step, project_aware=False, pasted_files=None):
        calls.append(role)
        state.user_role = role
        state.request_id = request_id
        state.project_aware = project_aware
        state.pasted_files = pasted_files or {}
        return state

    def resume(request_id, approved, **kwargs):
        calls.append((request_id, approved))
        state.status = "rejected" if not approved else "passed"
        return state

    pipe = SimpleNamespace(start=start, resume=resume, state=lambda _: state,
                           awaiting_approval=lambda _: True, run_dir=str(tmp_path / "runs"))
    monkeypatch.setattr(server, "_pipeline", pipe)
    return state, calls


@pytest.mark.parametrize("role", ["developer", "admin"])
@pytest.mark.parametrize("project_aware", [None, True, False])
def test_server_uses_member_role_and_only_submitter_can_approve(role, project_aware, team_client, monkeypatch, tmp_path):
    state, calls = fake_pipeline(monkeypatch, tmp_path)
    developer = team_client(role, "101")
    other_developer = team_client("developer", "102")
    files = {"Dockerfile": "FROM nginxinc/nginx-unprivileged:1.27-alpine\nEXPOSE 8080\n"}
    payload = {"prompt": "Deploy nginx to dev", "role": "admin", "pasted_files": files}
    if project_aware is not None:
        payload["project_aware"] = project_aware
    result = developer.post("/api/run", json=payload)
    assert result.status_code == 200
    run = result.json()
    assert calls == [role] and run["role"] == role and run["submitted_by"] == "101"
    assert auth.owner(run["request_id"]) == "101"
    assert state.project_aware is (True if project_aware is None else project_aware)
    assert state.pasted_files == files
    body = {"request_id": run["request_id"], "approved": False}
    assert other_developer.post("/api/approve", json=body).status_code == 403
    assert developer.post("/api/approve", json=body).status_code == 200
    assert calls[-1] == (run["request_id"], False)


def test_viewer_can_read_project_but_cannot_mutate(team_client, monkeypatch, tmp_path):
    fake_pipeline(monkeypatch, tmp_path)
    viewer = team_client("viewer")
    for path in ("/api/runs", "/events", "/api/guardrail-measurements"):
        assert viewer.get(path).status_code == 200
    for path, body in [("/api/run", {"prompt": "Deploy nginx"}),
                       ("/api/approve", {"request_id": "r", "approved": True}),
                       ("/api/pr/review", {"request_id": "r", "approved": True}),
                       ("/api/team", {"github_id": "12", "role": "admin"})]:
        assert viewer.post(path, json=body).status_code == 403
    assert viewer.get("/api/team").status_code == 403


def test_admin_assigns_roles_and_revocation_takes_effect_immediately(team_client):
    admin = team_client("admin", "1")
    developer = team_client("developer", "2")
    assert developer.post("/api/team", json={"github_id": "2", "role": "admin"}).status_code == 403
    assert admin.post("/api/team", json={"github_id": "2", "role": "viewer"}).status_code == 200
    assert developer.get("/api/session").json()["user"]["role"] == "viewer"
    assert developer.post("/api/run", json={"prompt": "Deploy nginx"}).status_code == 403
    assert admin.post("/api/team", json={"github_id": "2", "role": "viewer", "active": False}).status_code == 200
    assert developer.get("/events").status_code == 401
    with auth.database() as db:
        assert db.execute("SELECT COUNT(*) FROM membership_audit").fetchone()[0] == 2


def test_last_admin_cannot_be_removed_or_demoted(team_client):
    admin = team_client("admin", "1")
    assert admin.post("/api/team", json={"github_id": "1", "role": "viewer"}).status_code == 409
    assert admin.post("/api/team", json={"github_id": "1", "role": "admin", "active": False}).status_code == 409
    assert auth.member("1")["role"] == "admin"


def test_team_invite_resolves_username_to_immutable_id(team_client, monkeypatch):
    monkeypatch.setattr(auth, "_github_json", lambda url: {"id": 42, "login": "teammate"})
    admin = team_client("admin", "1")
    response = admin.post("/api/team", json={"github_id": "teammate", "role": "developer"})
    assert response.status_code == 200
    assert auth.member("42")["login"] == "teammate"
    assert auth.member("42")["role"] == "developer"


def test_slack_identity_requires_both_ids_and_cannot_be_shared(team_client):
    admin = team_client("admin", "1")
    body = {"github_id": "1", "role": "admin", "slack_user_id": "U123"}
    assert admin.post("/api/team", json=body).status_code == 422
    body["slack_team_id"] = "T123"
    assert admin.post("/api/team", json=body).status_code == 200
    body["github_id"] = "2"
    assert admin.post("/api/team", json=body).status_code == 409
    assert auth.slack_admin("T123", "U123")["github_id"] == "1"
    assert auth.slack_admin("OTHER", "U123") is None


def oauth_client(monkeypatch, github_id=1):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://infra.test")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("NL2INFRA_BOOTSTRAP_ADMIN_IDS", "1")
    calls = []

    def github(url, data=None, token=None):
        calls.append((url, data, token))
        return {"access_token": "secret-github-token"} if data else {"id": github_id, "login": "owner"}

    monkeypatch.setattr(auth, "_github_json", github)
    client = TestClient(server.app, base_url="https://infra.test")
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlparse(response.headers["location"]).query, keep_blank_values=True)
    assert params["scope"] == [""] and params["code_challenge_method"] == ["S256"]
    return client, params, calls


def test_github_sign_in_checks_state_pkce_and_sets_secure_revocable_session(monkeypatch):
    client, params, calls = oauth_client(monkeypatch)
    state = params["state"][0]
    assert client.get("/auth/callback", params={"code": "code", "state": "forged"}).status_code == 401
    assert calls == []
    response = client.get("/auth/callback", params={"code": "code", "state": state}, follow_redirects=False)
    assert response.status_code == 303
    verifier = calls[0][1]["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == params["code_challenge"][0]
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    assert "secret-github-token" not in response.headers["set-cookie"]
    assert client.get("/api/session").json()["user"]["role"] == "admin"
    assert client.get("/auth/callback", params={"code": "code", "state": state}, follow_redirects=False).status_code == 401


def test_github_sign_in_denies_uninvited_accounts(monkeypatch):
    client, params, _ = oauth_client(monkeypatch, github_id=2)
    response = client.get("/auth/callback", params={"code": "code", "state": params["state"][0]})
    assert response.status_code == 403
    assert client.get("/api/session").json()["user"] is None


def test_expired_oauth_state_is_not_exchanged(monkeypatch):
    client, params, calls = oauth_client(monkeypatch)
    with auth.database() as db:
        db.execute("UPDATE oauth_states SET expires=0")
    response = client.get("/auth/callback", params={"code": "code", "state": params["state"][0]})
    assert response.status_code == 401 and not calls


def test_bootstrap_env_cannot_restore_revoked_admin(monkeypatch):
    monkeypatch.setenv("NL2INFRA_BOOTSTRAP_ADMIN_IDS", "1,2")
    assert auth.member("1")["role"] == "admin"
    with auth.database() as db:
        db.execute("UPDATE members SET role='viewer' WHERE github_id='1'")
    assert auth.member("1")["role"] == "viewer"


def saved_run(tmp_path):
    path = tmp_path / "runs" / "pr.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"request_id": "pr", "status": "passed", "violations": [],
                               "pr_url": "https://github.com/acme/manifests/pull/42"}))
    return path


@pytest.mark.parametrize("role", ["developer", "viewer"])
def test_non_admin_review_never_calls_github(role, team_client, monkeypatch, tmp_path):
    saved_run(tmp_path)
    monkeypatch.setattr(slack, "_gitops_service", lambda: pytest.fail("Unauthorized GitHub call"))
    client = team_client(role)
    assert client.post("/api/pr/review", json={"request_id": "pr", "approved": True}).status_code == 403


def test_dashboard_and_slack_share_one_review_record(team_client, monkeypatch, tmp_path):
    path = saved_run(tmp_path)
    calls = []
    monkeypatch.setattr(slack, "_gitops_service", lambda: SimpleNamespace(merge_pull_request=lambda url: calls.append(url)))
    monkeypatch.setattr(slack, "_update_original", lambda *args: None)
    monkeypatch.setattr(server, "_pipeline", SimpleNamespace(run_dir=str(path.parent)))
    admin = team_client("admin", "1", "owner")
    with auth.database() as db:
        db.execute("UPDATE members SET slack_team_id='T123',slack_user_id='U123' WHERE github_id='1'")
    assert admin.post("/api/pr/review", json={"request_id": "pr", "approved": True}).status_code == 200
    payload = {"team": {"id": "T123"}, "user": {"id": "U123"},
               "actions": [{"action_id": "decline_pr", "value": "pr"}]}
    slack.process_interaction(payload, str(path.parent))
    second = admin.post("/api/pr/review", json={"request_id": "pr", "approved": False})
    assert second.json()["outcome"] == "approved" and len(calls) == 1
    record = json.loads(path.read_text())["slack_approval"]
    assert record["actor_id"] == "1" and record["source"] == "dashboard"
    assert admin.get("/api/run/pr").json()["slack_approval"]["outcome"] == "approved"


@pytest.mark.parametrize("role,active,team_id,user_id", [
    ("developer", 1, "T123", "U123"), ("viewer", 1, "T123", "U123"),
    ("admin", 0, "T123", "U123"), ("admin", 1, "OTHER", "U123"), ("admin", 1, "T123", "UNKNOWN")])
def test_slack_only_linked_active_admins_can_act(role, active, team_id, user_id, monkeypatch, tmp_path):
    path = saved_run(tmp_path)
    with auth.database() as db:
        db.execute("INSERT INTO members VALUES ('1','owner',?,?, 'T123','U123')", (role, active))
    monkeypatch.setattr(slack, "_gitops_service", lambda: pytest.fail("Unauthorized Slack merge"))
    denied = []
    monkeypatch.setattr(slack, "_deny_interaction", lambda url: denied.append(url))
    slack.process_interaction({"team": {"id": team_id}, "user": {"id": user_id, "username": "owner"},
        "actions": [{"action_id": "approve_merge", "value": "pr"}]}, str(path.parent))
    assert denied and "slack_approval" not in json.loads(path.read_text())


def test_queued_review_rechecks_admin_membership(team_client, monkeypatch, tmp_path):
    path = saved_run(tmp_path)
    team_client("viewer", "1")
    monkeypatch.setattr(slack, "_gitops_service", lambda: pytest.fail("Revoked admin must not merge"))
    slack.review_from_dashboard("pr", True, "1", str(path.parent))
    assert "slack_approval" not in json.loads(path.read_text())
