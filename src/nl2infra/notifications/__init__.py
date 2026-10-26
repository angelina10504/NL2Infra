"""In-dashboard notifications: a tiny event bus for the NL2Infra UI.

An event is a small record (id, timestamp, type, severity, run_id, message, read,
metadata). `emit_event` persists it to a JSONL store and publishes it to any live
Server-Sent-Events subscribers, so the dashboard shows alerts in real time.

Design note: `channels` is a pluggable dispatch table. Today only the built-in
"dashboard" channel exists (store + live push). Email or Slack can be added later by
registering one function with `register_channel("email", fn)` — no other code changes.
"""
import asyncio
import json
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

# --------------------------------------------------------------------------- schema

EVENT_TYPES = {
    "approval_required",
    "request_blocked",
    "fix_loop_exhausted",
    "pr_opened",
    "pr_merged",
    "deploy_succeeded",
    "deploy_failed",
    "high_failure_rate",
}

SEVERITIES = {"info", "warning", "critical", "success"}

DEFAULT_CHANNELS = ("dashboard",)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------- store

class _Store:
    """A JSONL file of events, one per line. Thread-safe; small enough to rewrite in full."""

    def __init__(self):
        self._lock = threading.Lock()

    def _path(self) -> str:
        # Resolved on each call so tests can redirect it with NL2INFRA_EVENTS_FILE.
        return os.environ.get("NL2INFRA_EVENTS_FILE", "events.jsonl")

    def append(self, event: dict) -> None:
        path = self._path()
        directory = os.path.dirname(path)
        with self._lock:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")

    def all(self) -> List[dict]:
        path = self._path()
        if not os.path.exists(path):
            return []
        events = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        return events

    def _rewrite(self, events: List[dict]) -> None:
        path = self._path()
        directory = os.path.dirname(path)
        with self._lock:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")

    def mark_read(self, event_id: str) -> bool:
        events = self.all()
        found = False
        for event in events:
            if event.get("id") == event_id:
                event["read"] = True
                found = True
        if found:
            self._rewrite(events)
        return found

    def mark_all_read(self) -> int:
        events = self.all()
        changed = 0
        for event in events:
            if not event.get("read"):
                event["read"] = True
                changed += 1
        if changed:
            self._rewrite(events)
        return changed


_store = _Store()


# --------------------------------------------------------------------------- broker

class _Broker:
    """Fans each new event out to the queues of all live SSE subscribers."""

    def __init__(self, maxsize: int = 1000):
        self._subscribers: "set[queue.Queue]" = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a slow client drops events rather than blocking the pipeline

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


_broker = _Broker()


# --------------------------------------------------------------------------- channels

def _dashboard_channel(event: dict) -> None:
    """The built-in channel: persist the event and push it to live subscribers."""
    _store.append(event)
    _broker.publish(event)


CHANNELS: Dict[str, Callable[[dict], None]] = {"dashboard": _dashboard_channel}


def register_channel(name: str, handler: Callable[[dict], None]) -> None:
    """Add a delivery channel (e.g. email, Slack). Call once at startup."""
    CHANNELS[name] = handler


# --------------------------------------------------------------------------- emit / query

def emit_event(type: str, severity: str, run_id: Optional[str], message: str,
               metadata: Optional[dict] = None, channels=None) -> dict:
    """Create an event and deliver it over the given channels (default: dashboard).

    `type` and `severity` are validated against the known sets so a typo surfaces early.
    Returns the created event. Never raises for an unknown channel; it is skipped.
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type}")
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity}")
    event = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "type": type,
        "severity": severity,
        "run_id": run_id,
        "message": message,
        "read": False,
        "metadata": metadata or {},
    }
    for name in (channels or DEFAULT_CHANNELS):
        handler = CHANNELS.get(name)
        if handler:
            handler(event)
    return event


def emit_event_safe(*args, **kwargs) -> Optional[dict]:
    """emit_event that never raises, for use inside pipeline nodes."""
    try:
        return emit_event(*args, **kwargs)
    except Exception:
        return None


def list_events(unread_only: bool = False, limit: Optional[int] = None) -> List[dict]:
    """Events newest-first, optionally only the unread ones."""
    events = _store.all()
    if unread_only:
        events = [e for e in events if not e.get("read")]
    events = list(reversed(events))
    if limit is not None:
        events = events[:limit]
    return events


def unread_count() -> int:
    return sum(1 for e in _store.all() if not e.get("read"))


def mark_read(event_id: str) -> bool:
    return _store.mark_read(event_id)


def mark_all_read() -> int:
    return _store.mark_all_read()


# --------------------------------------------------------------------------- SSE

def subscribe():
    """A queue that receives every event published after this call. Unsubscribe when done."""
    return _broker.subscribe()


def unsubscribe(q) -> None:
    _broker.unsubscribe(q)


def subscriber_count() -> int:
    return _broker.subscriber_count()


async def event_stream(poll: float = 0.5, heartbeat_after: int = 30):
    """An async generator of SSE frames: a greeting ping, then one frame per new event.

    Polls the subscriber queue on a short timeout so that, when the client disconnects and
    the server cancels this generator, cancellation lands promptly and `finally` unsubscribes.
    """
    q = subscribe()
    try:
        yield "event: ping\ndata: {}\n\n"
        idle = 0
        while True:
            try:
                event = await asyncio.to_thread(q.get, True, poll)
                yield f"data: {json.dumps(event)}\n\n"
                idle = 0
            except queue.Empty:
                idle += 1
                if idle >= heartbeat_after:  # ~heartbeat_after*poll seconds of silence
                    idle = 0
                    yield "event: ping\ndata: {}\n\n"
    finally:
        unsubscribe(q)


def reset_for_tests() -> None:
    """Drop all live subscribers. Tests isolate the store via NL2INFRA_EVENTS_FILE."""
    with _broker._lock:
        _broker._subscribers.clear()
