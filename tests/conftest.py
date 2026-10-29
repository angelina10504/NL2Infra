import sys
import os

import pytest

# Add project root and src/nl2infra to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_dir = os.path.join(root_dir, "src", "nl2infra")

for p in [root_dir, src_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _isolate_events(tmp_path, monkeypatch):
    """Point the notifications store at a per-test file so emits never pollute a shared log."""
    monkeypatch.setenv("NL2INFRA_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("NL2INFRA_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("NL2INFRA_BOOTSTRAP_ADMIN_IDS", "")
    monkeypatch.delenv("NL2INFRA_AUTH_BASE_URL", raising=False)
    import notifications
    notifications.reset_for_tests()
    yield
    notifications.reset_for_tests()


@pytest.fixture
def team_client():
    """Real sessions for HTTP tests; no authentication bypass in application code."""
    import auth
    import server
    from fastapi.testclient import TestClient

    def make(role="admin", github_id="100", login="test.member"):
        with auth.database() as db:
            db.execute("INSERT OR REPLACE INTO members(github_id,login,role) VALUES (?,?,?)", (github_id, login, role))
        token, csrf = auth.create_session(github_id)
        client = TestClient(server.app)
        client.cookies.set(auth.SESSION_COOKIE, token)
        client.headers["X-CSRF-Token"] = csrf
        return client
    return make
