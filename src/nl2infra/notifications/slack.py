"""Slack delivery and signed interaction handling for pull request approvals."""
import hashlib
import hmac
import json
import logging
import os
import re
try:
    import fcntl  # POSIX-only; used for the cross-process run-file lock below.
except ImportError:
    fcntl = None   # Windows has no fcntl; the lock degrades to a no-op (single-process laptop).
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_action_lock = threading.Lock()
_PROCESSING = "processing"


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str,
                     now: float | None = None) -> bool:
    """Verify Slack's v0 signature and reject stale or future timestamps."""
    if not secret or not timestamp or not signature:
        return False
    if not timestamp.isascii() or not timestamp.isdigit() or not signature.isascii():
        return False
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_time = time.time() if now is None else now
    if abs(current_time - timestamp_value) > 300:
        return False
    base = b"v0:" + timestamp.encode("ascii", errors="strict") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def parse_interaction_body(body: bytes) -> dict:
    """Decode the form-encoded payload Slack sends for interactive components."""
    fields = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
    raw_payload = fields.get("payload", [None])[0]
    if not raw_payload:
        raise ValueError("missing Slack payload")
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise ValueError("invalid Slack payload")
    return payload


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": str(text)[:3000], "emoji": False}


def build_approval_message(event: dict, run: dict, public_base_url: str = "") -> dict:
    metadata = event.get("metadata") or {}
    plan = run.get("approved_plan") or run.get("plan") or {}
    resources = plan.get("resources") or []
    namespaces = sorted({str(resource.get("namespace")) for resource in resources
                         if resource.get("namespace")})
    namespace = ", ".join(namespaces) if namespaces else "not specified"
    rounds = run.get("rounds") or []
    final_violations = rounds[-1].get("violations", []) if rounds else run.get("violations", [])
    files = rounds[-1].get("files") if rounds else None
    files = files or run.get("files") or {}
    request_id = str(run.get("request_id") or event.get("run_id") or "")
    pr_url = str(run.get("pr_url") or metadata.get("pr_url") or "")
    prompt = str(run.get("user_prompt") or run.get("prompt") or "Request text unavailable")
    role = str(run.get("user_role") or run.get("role") or "unknown")
    fix_rounds = int(run.get("iterations", run.get("fix_rounds", 0)) or 0)

    blocks = [
        {"type": "header", "text": _plain("NL2Infra pull request approval")},
        {"type": "section", "text": _plain(prompt)},
        {"type": "section", "fields": [
            _plain(f"Role\n{role}"), _plain(f"Target namespace\n{namespace}"),
        ]},
        {"type": "section", "fields": [
            _plain("Gauntlet checks\nCheckov · OPA/Conftest · dry-run · plan conformance"),
            _plain(f"Final violations\n{len(final_violations)} · fix rounds {fix_rounds}"),
        ]},
        {"type": "section", "text": _plain("Files changed\n" + ("\n".join(files) if files else "No files recorded"))},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"<{pr_url}|Open pull request>"}},
        {"type": "actions", "block_id": f"nl2infra_{request_id}", "elements": [
            {"type": "button", "action_id": "approve_merge", "text": _plain("Approve and merge"),
             "style": "primary", "value": request_id},
            {"type": "button", "action_id": "decline_pr", "text": _plain("Decline"),
             "style": "danger", "value": request_id},
        ]},
    ]
    if public_base_url:
        blocks.insert(6, {"type": "context", "elements": [
            _plain(f"Approval actions are verified by {public_base_url.rstrip('/')}/slack/interactions")
        ]})
    return {"text": f"Pull request approval requested for {request_id}", "blocks": blocks}


def _post_json(url: str, payload: dict, timeout: float = 3.0) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        response.read(1)


def _read_run(request_id: str, run_dir: str) -> dict | None:
    path = Path(run_dir) / f"{request_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def notify_pr_opened(event: dict) -> None:
    """Queue Slack delivery without holding up the pipeline event emitter."""
    if not os.environ.get("SLACK_WEBHOOK_URL"):
        return
    request_id = event.get("run_id")
    if not request_id:
        return
    run_dir = os.environ.get("NL2INFRA_RUNS_DIR", "runs")

    def deliver_when_saved():
        run = None
        for _ in range(60):
            run = _read_run(str(request_id), run_dir)
            if run:
                break
            time.sleep(0.05)
        if not run:
            logger.warning("Slack PR notification skipped: run context unavailable (%s)", request_id)
            return
        payload = build_approval_message(event, run, os.environ.get("PUBLIC_BASE_URL", ""))
        try:
            _post_json(os.environ["SLACK_WEBHOOK_URL"], payload, timeout=3.0)
        except Exception:
            # Webhook failures must never affect the run or disclose webhook credentials.
            logger.warning("Slack PR notification delivery failed for run %s", request_id)

    threading.Thread(target=deliver_when_saved, name=f"slack-pr-{request_id}", daemon=True).start()


def _run_path(request_id: str, run_dir: str) -> Path:
    return Path(run_dir) / f"{request_id}.json"


def _write_run(path: Path, run: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(run, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _run_file_lock(path: Path):
    """Serialize approval reservations across workers sharing the same run directory."""
    lock_path = path.with_suffix(path.suffix + ".slack-lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _gitops_service():
    from gitops import GitOpsService
    return GitOpsService()


def _response_blocks(outcome: str, actor: str, reason: str = "") -> list[dict]:
    if outcome == "approved":
        message = f"Approved and merged by {actor}."
    elif outcome == "declined":
        message = f"Declined by {actor}. Reason: {reason}"
    elif outcome == _PROCESSING:
        message = "This approval action is already being processed; no second GitHub action was started."
    else:
        message = "The approval action could not be completed. Contact the NL2Infra owner before retrying."
    return [{"type": "section", "text": _plain(message[:2900])}]


def _update_original(response_url: str, outcome: str, actor: str, reason: str = "") -> None:
    parsed = urlparse(response_url or "")
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com":
        return
    try:
        _post_json(response_url, {
            "replace_original": True,
            "text": f"NL2Infra pull request {outcome}",
            "blocks": _response_blocks(outcome, actor, reason),
        }, timeout=3.0)
    except Exception:
        logger.warning("Slack approval message update failed")


def _actor(payload: dict) -> str:
    user = payload.get("user") or {}
    return str(user.get("username") or user.get("name") or user.get("id") or "Slack user")


def _deny_interaction(response_url: str) -> None:
    """Tell only the clicking user; preserve the shared message and its buttons."""
    parsed = urlparse(response_url or "")
    if parsed.scheme != "https" or parsed.hostname != "hooks.slack.com":
        return
    try:
        _post_json(response_url, {"response_type": "ephemeral", "replace_original": False,
                                 "text": "Only an active NL2Infra admin with a linked Slack identity may review this PR."})
    except Exception:
        logger.warning("Slack permission response could not be delivered")


def _process_interaction(payload: dict, run_dir: str | None = None) -> None:
    """Apply one verified button action and persist the terminal decision once."""
    run_dir = run_dir or os.environ.get("NL2INFRA_RUNS_DIR", "runs")
    actions = payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id")
    request_id = str(action.get("value") or "")
    response_url = str(payload.get("response_url") or "")
    import auth
    user = auth.slack_admin(str((payload.get("team") or {}).get("id") or ""),
                            str((payload.get("user") or {}).get("id") or ""))
    if not user:
        _deny_interaction(response_url)
        return
    actor = user["login"] or user["github_id"]
    requested_outcome = "approved" if action_id == "approve_merge" else "declined" if action_id == "decline_pr" else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) or not requested_outcome:
        _update_original(response_url, "failed", actor)
        return

    _apply_decision(request_id, requested_outcome, user["github_id"], actor,
                    run_dir, response_url, "Slack")


def review_from_dashboard(request_id: str, approved: bool, github_id: str, run_dir: str) -> None:
    """Recheck membership in the background, after the HTTP request was acknowledged."""
    import auth
    user = auth.member(github_id)
    if not user or user["role"] != "admin":
        return
    _apply_decision(request_id, "approved" if approved else "declined", github_id,
                    user["login"] or github_id, run_dir, "", "dashboard")


def _apply_decision(request_id: str, requested_outcome: str, github_id: str, actor: str,
                    run_dir: str, response_url: str, source: str) -> None:
    import auth
    user = auth.member(github_id)
    if not user or user["role"] != "admin":
        _deny_interaction(response_url)
        return

    path = _run_path(request_id, run_dir)
    with _action_lock, _run_file_lock(path):
        run = _read_run(request_id, run_dir)
        if not run:
            _update_original(response_url, "failed", actor)
            return
        prior = run.get("slack_approval") or {}
        if prior.get("outcome") in ("approved", "declined", "failed"):
            _update_original(response_url, prior["outcome"], prior.get("actor", actor), prior.get("reason", ""))
            return
        if prior.get("outcome") == _PROCESSING:
            _update_original(response_url, _PROCESSING, actor)
            return

        final_violations = ((run.get("rounds") or [{}])[-1]).get("violations", run.get("violations", []))
        if run.get("status") != "passed" or final_violations:
            _update_original(response_url, "failed", actor)
            return

        reason = f"Declined via {source} by {actor}" if requested_outcome == "declined" else ""
        record = {
            "outcome": _PROCESSING,
            "requested_outcome": requested_outcome,
            "actor": actor,
            "actor_id": github_id,
            "source": source,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        run["slack_approval"] = record
        _write_run(path, run)

    try:
        # Permission changes take effect even while an action is queued.
        current = auth.member(github_id)
        if not current or current["role"] != "admin":
            raise PermissionError("Admin permission was revoked")
        pr_url = run.get("pr_url")
        if not isinstance(pr_url, str) or not pr_url.startswith("https://"):
            raise ValueError("run has no public pull request URL")
        gitops = _gitops_service()
        if requested_outcome == "approved":
            gitops.merge_pull_request(pr_url)
        else:
            gitops.close_pull_request(pr_url)
        record["outcome"] = requested_outcome
        run["slack_approval"] = record
        _write_run(path, run)
        import notifications
        if requested_outcome == "approved":
            notifications.emit_event_safe("pr_merged", "success", request_id,
                                           f"Pull request approved and merged by {actor}",
                                           metadata={"pr_url": pr_url, "actor": actor})
        else:
            notifications.emit_event_safe("pr_declined", "warning", request_id,
                                           f"Pull request declined by {actor}: {reason}",
                                           metadata={"pr_url": pr_url, "actor": actor, "reason": reason})
        _update_original(response_url, requested_outcome, actor, reason)
    except Exception:
        # Keep an uncertain action terminal to avoid accidentally merging twice on retry.
        record["outcome"] = "failed"
        record["reason"] = reason or "GitHub action failed; outcome may require manual verification"
        run["slack_approval"] = record
        try:
            _write_run(path, run)
        except OSError:
            pass
        import notifications
        notifications.emit_event_safe("pr_approval_failed", "warning", request_id,
                                       f"Pull request approval action failed for run {request_id}",
                                       metadata={"actor": actor})
        _update_original(response_url, "failed", actor)
        logger.warning("Slack GitHub approval action failed for run %s", request_id)


def process_interaction(payload: dict, run_dir: str | None = None) -> None:
    """Background-task boundary: unexpected storage errors still get a safe Slack ack."""
    try:
        _process_interaction(payload, run_dir)
    except Exception:
        actions = payload.get("actions") or [] if isinstance(payload, dict) else []
        request_id = actions[0].get("value") if actions and isinstance(actions[0], dict) else "unknown"
        response_url = payload.get("response_url", "") if isinstance(payload, dict) else ""
        try:
            actor = _actor(payload) if isinstance(payload, dict) else "Slack user"
        except Exception:
            actor = "Slack user"
        logger.warning("Slack interaction could not be completed for run %s", request_id)
        _update_original(str(response_url), "failed", actor)


def slack_channel(event: dict) -> None:
    if event.get("type") != "pr_opened":
        return
    try:
        notify_pr_opened(event)
    except Exception:
        logger.warning("Slack PR notification could not be queued for run %s", event.get("run_id"))
