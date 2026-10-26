"""Notification bus tests: emit, list, mark-read, channels and the SSE stream.

The autouse `_isolate_events` fixture (tests/conftest.py) points the JSONL store at a
per-test file, so these never touch a shared events log.
"""
import asyncio
import json

import pytest

import notifications
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import server
    return TestClient(server.app)


# ------------------------------------------------------------------ emit

def test_emit_event_returns_a_well_formed_event():
    event = notifications.emit_event("pr_opened", "success", "req-1", "PR opened", metadata={"pr_url": "x"})
    assert set(event) == {"id", "timestamp", "type", "severity", "run_id", "message", "read", "metadata"}
    assert event["type"] == "pr_opened" and event["severity"] == "success"
    assert event["run_id"] == "req-1" and event["read"] is False
    assert event["metadata"] == {"pr_url": "x"}


def test_emit_rejects_unknown_type_and_severity():
    with pytest.raises(ValueError):
        notifications.emit_event("not_a_type", "info", "r", "m")
    with pytest.raises(ValueError):
        notifications.emit_event("pr_opened", "loud", "r", "m")


def test_emit_event_safe_never_raises():
    assert notifications.emit_event_safe("bad_type", "info", "r", "m") is None


# ------------------------------------------------------------------ list / count

def test_list_events_is_newest_first_and_persisted():
    notifications.emit_event("request_blocked", "warning", "r1", "first")
    notifications.emit_event("approval_required", "info", "r2", "second")
    events = notifications.list_events()
    assert [e["message"] for e in events] == ["second", "first"]


def test_unread_only_and_limit():
    notifications.emit_event("request_blocked", "warning", "r1", "a")
    notifications.emit_event("request_blocked", "warning", "r2", "b")
    notifications.emit_event("request_blocked", "warning", "r3", "c")
    assert notifications.unread_count() == 3
    assert len(notifications.list_events(limit=2)) == 2
    assert len(notifications.list_events(unread_only=True)) == 3


# ------------------------------------------------------------------ mark read

def test_mark_read_single():
    event = notifications.emit_event("pr_opened", "success", "r", "m")
    assert notifications.mark_read(event["id"]) is True
    assert notifications.unread_count() == 0
    assert notifications.mark_read("missing") is False


def test_mark_all_read():
    notifications.emit_event("pr_opened", "success", "r", "a")
    notifications.emit_event("pr_opened", "success", "r", "b")
    assert notifications.mark_all_read() == 2
    assert notifications.unread_count() == 0


# ------------------------------------------------------------------ channels

def test_register_channel_receives_events():
    seen = []
    notifications.register_channel("test_sink", lambda e: seen.append(e))
    try:
        notifications.emit_event("pr_merged", "success", "r", "m", channels=["dashboard", "test_sink"])
    finally:
        notifications.CHANNELS.pop("test_sink", None)
    assert len(seen) == 1 and seen[0]["type"] == "pr_merged"


# ------------------------------------------------------------------ broker (SSE core)

def test_broker_delivers_published_event_to_subscribers():
    q = notifications.subscribe()
    try:
        notifications.emit_event("deploy_succeeded", "success", "r", "live")
        event = q.get(timeout=2.0)
        assert event["message"] == "live" and event["type"] == "deploy_succeeded"
    finally:
        notifications.unsubscribe(q)


# ------------------------------------------------------------------ HTTP endpoints

def test_events_endpoints_list_and_mark_read(client):
    event = notifications.emit_event("request_blocked", "warning", "r", "blocked")
    body = client.get("/events").json()
    assert body["unread"] == 1 and body["events"][0]["message"] == "blocked"

    assert client.get("/events", params={"unread_only": True}).json()["unread"] == 1
    res = client.post(f"/events/{event['id']}/read").json()
    assert res["ok"] and res["unread"] == 0
    assert client.post("/events/some-missing/read").status_code == 404

    notifications.emit_event("pr_opened", "success", "r", "x")
    assert client.post("/events/read-all").json()["marked"] == 1


def test_sse_stream_pushes_a_new_event():
    """Drive the SSE generator: read the greeting ping (so the subscriber exists), emit an
    event, and confirm the next frame carries it. Wrapped in a hard timeout."""
    async def scenario():
        gen = notifications.event_stream(poll=0.1)
        greeting = await gen.__anext__()
        assert greeting.startswith("event: ping")
        notifications.emit_event("approval_required", "info", "req-sse", "needs approval")
        try:
            while True:
                frame = await gen.__anext__()
                if frame.startswith("data: ") and frame.strip() != "data: {}":
                    payload = json.loads(frame[len("data: "):].strip())
                    assert payload["message"] == "needs approval"
                    return
        finally:
            await gen.aclose()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_sse_endpoint_is_registered_as_event_stream():
    import server
    route = next(r for r in server.app.routes if getattr(r, "path", None) == "/events/stream")
    assert route is not None


def test_api_run_emits_approval_required_over_http(monkeypatch):
    """The real server path: POST /api/run pauses for approval and the bell feed shows it."""
    monkeypatch.setenv("MOCK_MODE", "true")
    import server
    server._pipeline = None  # rebuild with mock services under this test's events store
    client = TestClient(server.app)
    run = client.post("/api/run", json={"prompt": "Deploy nginx to dev", "role": "developer"}).json()
    assert run["status"] == "awaiting_approval"
    feed = client.get("/events").json()
    assert feed["unread"] >= 1
    assert any(e["type"] == "approval_required" and e["run_id"] == run["request_id"] for e in feed["events"])
    server._pipeline = None
