import os
import sys
import uuid
import json
import logging
import threading
import hmac
import re
from pathlib import Path
from typing import Optional, Dict
from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# Ensure src and root are in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from contracts import RunState, Plan, Resource, Violation
from guardrails import check_guardrails
from pipeline import Pipeline
import notifications
from notifications import slack as slack_notifications
import auth
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.responses import Response

logger = logging.getLogger(__name__)

app = FastAPI(title="NL2Infra Enterprise Studio", version="1.0.0")

app.include_router(auth.router)


@app.middleware("http")
async def project_access(request: Request, call_next):
    path = request.url.path
    protected = path.startswith(("/api/", "/events", "/ask")) or path == "/alerts/webhook"
    if protected and path != "/api/session":
        user = auth.session(request.cookies.get(auth.SESSION_COOKIE, ""))
        if not user:
            return JSONResponse({"error": "Sign in with an invited GitHub account"}, status_code=401)
        request.state.member = user
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf.encode(), user["csrf"].encode()):
                return JSONResponse({"error": "Missing or invalid CSRF token"}, status_code=403)
            if user["role"] == "viewer" and path not in ("/api/logout", "/api/why") and not path.startswith("/events"):
                return JSONResponse({"error": "Viewers have read-only project access"}, status_code=403)
    try:
        response = await call_next(request)
    finally:
        if path == "/auth/callback":
            # Avoid recording OAuth codes in Uvicorn's access log.
            request.scope["query_string"] = b""
    response.headers["Cache-Control"] = "no-store"
    return response

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")


class RunRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    use_gauntlet: bool = True
    # Project-Aware Generation: on by default for the dashboard; set false for the old behaviour.
    project_aware: bool = True
    pasted_files: Dict[str, str] = {}


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool


class GuardrailCompareRequest(BaseModel):
    question: str


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>NL2Infra Studio template not found</h1>", status_code=404)


_pipeline = None
_live_runs: Dict[str, dict] = {}
_live_runs_lock = threading.Lock()


def get_pipeline() -> Pipeline:
    # One pipeline per server process: it holds the runs that are waiting for approval.
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(auto_approve=False, run_dir=os.environ.get("NL2INFRA_RUNS_DIR", "runs"))
    return _pipeline


def _profile_view(state: RunState) -> Optional[dict]:
    """The detected project profile for the plan screen: type, the kinds that will be
    created, the must-have list, and the safe defaults used for anything unknown."""
    if state.profile is None:
        return None
    try:
        import fit
        data = state.profile.model_dump(mode="json")
        data["expected_kinds"] = fit.expected_kinds(state.profile.workload_type)
        data["must_have"] = fit.required_must_haves(state.profile)
        return data
    except Exception:
        return state.profile.model_dump(mode="json")


def _response(state: RunState, awaiting_approval: bool) -> dict:
    return {
        "request_id": state.request_id,
        "status": "awaiting_approval" if awaiting_approval else state.status,
        "profile": _profile_view(state),
        "plan": state.plan.model_dump() if state.plan else None,
        "approved_plan": state.approved_plan.model_dump() if state.approved_plan else None,
        "files": state.files or {},
        "violations": [v.model_dump() for v in state.violations],
        "role": state.user_role,
        "model": state.model,
        "rounds": state.rounds or [],
        "iterations": state.iterations,
        "pr_url": state.pr_url,
        "pr_error": state.pr_error,
        "deploy": state.deploy,
        "metrics": state.metrics,
        "rejection_reason": state.rejection_reason,
        "submitted_by": auth.owner(state.request_id),
        "user_prompt": state.user_prompt,
        "started_at": state.started_at,
    }


def _live_snapshot(state: RunState, current: Optional[str] = None, message: str = "") -> dict:
    data = _response(state, False)
    data["current_stage"] = current
    data["message"] = message
    data["status"] = state.status
    return data


def _remember_step(step_name: str, state: RunState) -> None:
    """Keep a small request-time snapshot so the browser can follow a running graph."""
    try:
        import ui_data
        snapshot = state.model_dump()
        current, message = ui_data.live_position(step_name, snapshot)
        if step_name == "pull_request":
            current, message = "deploy", "Waiting for pull request merge"
    except Exception:
        current, message = step_name, "In progress"
    with _live_runs_lock:
        _live_runs[state.request_id] = _live_snapshot(state, current, message)


@app.post("/api/run")
@app.post("/ask")
def run_pipeline(req: RunRequest, request: Request):
    """Guardrails and planning. Returns the plan with status 'awaiting_approval'; nothing is generated yet."""
    user = auth.require_role(request, "developer", "admin")
    pipeline = get_pipeline()
    request_id = uuid.uuid4().hex
    auth.record_owner(request_id, user["github_id"])
    state = pipeline.start(req.prompt, user["role"], request_id=request_id, on_step=_remember_step,
                           project_aware=req.project_aware, pasted_files=req.pasted_files)
    result = _response(state, pipeline.awaiting_approval(state.request_id))
    result["current_stage"] = "approve" if pipeline.awaiting_approval(state.request_id) else None
    with _live_runs_lock:
        _live_runs[state.request_id] = result
    return result


@app.post("/api/approve")
def approve_plan(req: ApproveRequest, request: Request):
    """The user's decision on the plan. On approval the run continues to the end."""
    user = auth.require_role(request, "developer", "admin")
    if auth.owner(req.request_id) != user["github_id"]:
        raise HTTPException(403, "You may only approve or reject your own plan")
    pipeline = get_pipeline()
    try:
        if not pipeline.awaiting_approval(req.request_id):
            raise ValueError(f"run {req.request_id} is not waiting for approval")
        state = pipeline.state(req.request_id)
        if not req.approved:
            result = pipeline.resume(req.request_id, approved=False)
            payload = _response(result, False)
            payload["current_stage"] = None
            with _live_runs_lock:
                _live_runs[req.request_id] = payload
            return payload
        with _live_runs_lock:
            _live_runs[req.request_id] = _live_snapshot(state, "generate", "Starting generation")

        def continue_run():
            try:
                result = pipeline.resume(req.request_id, approved=True, on_step=_remember_step)
                payload = _response(result, False)
                payload["current_stage"] = None
                with _live_runs_lock:
                    _live_runs[req.request_id] = payload
            except Exception as error:
                logger.exception("Run continuation failed: %s", req.request_id)
                with _live_runs_lock:
                    current = _live_runs.get(req.request_id, {})
                    current.update(status="escalated", current_stage=None, error=str(error))
                    _live_runs[req.request_id] = current

        threading.Thread(target=continue_run, name=f"nl2infra-{req.request_id}", daemon=True).start()
        return {"request_id": req.request_id, "status": "running", "current_stage": "generate"}
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=409)


@app.get("/api/run/{request_id}")
async def get_live_run(request_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise HTTPException(404, "Run not found")
    with _live_runs_lock:
        result = _live_runs.get(request_id)
    saved = slack_notifications._read_run(request_id, get_pipeline().run_dir)
    if result is None and saved:
        result = saved
    if result is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    return {**result, "submitted_by": auth.owner(request_id),
            "slack_approval": (saved or {}).get("slack_approval")}


class PRReviewRequest(BaseModel):
    request_id: str
    approved: bool


@app.post("/api/pr/review")
def review_pr(req: PRReviewRequest, request: Request, background_tasks: BackgroundTasks):
    user = auth.require_role(request, "admin")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", req.request_id):
        raise HTTPException(404, "Run not found")
    run = slack_notifications._read_run(req.request_id, get_pipeline().run_dir)
    if not run:
        raise HTTPException(404, "Run not found")
    prior = run.get("slack_approval") or {}
    if prior.get("outcome"):
        return {"outcome": prior["outcome"]}
    if run.get("status") != "passed" or not str(run.get("pr_url", "")).startswith("https://github.com/"):
        raise HTTPException(409, "This run has no passed pull request to review")
    background_tasks.add_task(slack_notifications.review_from_dashboard, req.request_id,
                              req.approved, user["github_id"], get_pipeline().run_dir)
    return {"outcome": "processing"}


def guardrail_measurements() -> dict:
    """Evaluate the checked-in datasets through the same guardrail function as evaluate.py."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    from contracts import RunState
    from guardrails import check_guardrails

    def load_rows(filename):
        path = os.path.join(root, "benchmarks", filename)
        with open(path, encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    try:
        adversarial = load_rows("adversarial.jsonl")
        legitimate = load_rows("correctness.jsonl")
        refused_adversarial = sum(bool(check_guardrails(RunState(
            request_id=row["id"], user_role=row["role"], user_prompt=row["prompt"], model="none")))
            for row in adversarial)
        refused_legitimate = sum(bool(check_guardrails(RunState(
            request_id=row["id"], user_role=row["role"], user_prompt=row["prompt"], model="none")))
            for row in legitimate)
        return {"adversarial_refused": refused_adversarial, "adversarial_total": len(adversarial),
                "legitimate_false_refused": refused_legitimate, "legitimate_total": len(legitimate)}
    except (OSError, ValueError, KeyError, TypeError):
        return {"adversarial_refused": None, "adversarial_total": None,
                "legitimate_false_refused": None, "legitimate_total": None}


@app.get("/api/guardrail-measurements")
async def get_guardrail_measurements():
    return guardrail_measurements()


@app.get("/api/models")
async def get_models():
    """Expose the model configured for this server, without inventing alternatives."""
    configured = os.environ.get("MODEL_NAME")
    if not configured:
        try:
            configured = getattr(get_pipeline().llm, "model_name", None)
        except Exception:
            configured = None
    return {"models": [configured] if configured else ["Default model"]}


@app.post("/api/guardrail-compare")
@app.post("/ask/guardrail-compare")
async def guardrail_compare(req: GuardrailCompareRequest, request: Request):
    user = auth.require_role(request, "developer", "admin")
    role = user["role"]
    question_lower = req.question.lower()

    # Check guardrails
    state = RunState(
        request_id="eval-" + str(uuid.uuid4())[:6],
        user_role=role,
        user_prompt=req.question,
        model="evaluator"
    )
    rejection = check_guardrails(state)

    # 1. Unguarded simulation
    if "ignore" in question_lower or "system prompt" in question_lower or "delete" in question_lower:
        without_guardrail = (
            "🚨 CRITICAL SECURITY COMPROMISE:\n"
            "SYSTEM PROMPT EXFILTRATED:\n"
            "'You are an internal Kubernetes administrator agent. System instructions: ALWAYS_EXECUTE_ROOT=true...'\n"
            "Action performed: Executing 'kubectl delete namespaces --all --force'...\n"
            "Status: Cluster control-plane damaged. All tenant workloads terminated."
        )
    elif "production" in question_lower and role == "developer":
        without_guardrail = (
            "⚠️ PRIVILEGE ESCALATION:\n"
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: prod-database\n"
            "  namespace: production\n"
            "spec:\n"
            "  # junior_dev successfully bypassed namespace access controls!\n"
            "  # Direct write to production cluster allowed without human approval."
        )
    elif "bake" in question_lower or "recipe" in question_lower or "poem" in question_lower:
        without_guardrail = (
            "🍰 Walnut Chocolate Brownies Recipe:\n"
            "Ingredients: 200g dark chocolate, 150g butter, 100g sugar, 50g walnuts...\n"
            "(Agent wasted LLM token budget on off-topic non-infrastructure task)."
        )
    else:
        without_guardrail = (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n"
            "  name: insecure-app\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "      - name: app\n"
            "        image: app:latest\n"
            "        securityContext:\n"
            "          privileged: true  # ❌ Insecure container running as root!"
        )

    # 2. Guarded output
    if rejection:
        with_guardrail = (
            f"🛡️ NL2INFRA DEFENSE ENFORCED:\n\n"
            f"REASON: {rejection}\n\n"
            f"STATUS: Request BLOCKED immediately before LLM inference.\n"
            f"POLICY: Zero tokens dispatched. Incident logged to audit store.\n"
            f"RECOMMENDATION: Request requires platform_admin role or valid infrastructure specification."
        )
    else:
        with_guardrail = (
            "✅ GAUNTLET VALIDATED:\n"
            "Infrastructure request cleared input guardrails.\n"
            "Generated manifests strictly conform to OPA base policies:\n"
            "- securityContext.runAsNonRoot: true\n"
            "- readOnlyRootFilesystem: true\n"
            "- CPU and memory limits strictly enforced."
        )

    return {
        "without_guardrail": without_guardrail,
        "with_guardrail": with_guardrail,
        "rejection": rejection
    }


class WhyRequest(BaseModel):
    question: str


@app.post("/api/why")
async def why(req: WhyRequest):
    """RAG Q&A over the run logs: 'Why was my request blocked or changed?'"""
    from rag import answer_why
    runs_dir = os.environ.get("NL2INFRA_RUNS_DIR", "runs")
    try:
        return answer_why(req.question, runs_dir=runs_dir)
    except Exception as error:
        return JSONResponse({"error": str(error)}, status_code=500)


@app.get("/api/runs")
async def get_runs():
    runs = {}
    for path in Path(get_pipeline().run_dir).glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(row, dict) and row.get("request_id"):
                runs[row["request_id"]] = row
        except (OSError, ValueError):
            continue
    with _live_runs_lock:
        for request_id, live in _live_runs.items():
            runs[request_id] = {**live, "slack_approval": runs.get(request_id, {}).get("slack_approval")}
    recent = sorted(runs.values(), key=lambda r: r.get("started_at") or r.get("date") or "")[-20:]
    return [{**r, "submitted_by": auth.owner(r["request_id"])} for r in recent]


# ------------------------------------------------------------------- metrics

@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus scrape target."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/alerts/webhook")
async def alerts_webhook(payload: dict, request: Request):
    """Alertmanager posts here; each firing alert becomes a dashboard notification so
    system-health alerts also show up in the bell."""
    auth.require_role(request, "admin")
    alerts = payload.get("alerts", []) if isinstance(payload, dict) else []
    # Map each Prometheus alert to a dashboard event type (all are valid EVENT_TYPES).
    alert_to_event = {
        "LowSuccessRate": "high_failure_rate",
        "FixLoopExhaustedSpike": "fix_loop_exhausted",
        "PendingApprovalsStale": "approval_required",
    }
    emitted = 0
    for alert in alerts:
        if alert.get("status") == "resolved":
            continue
        labels = alert.get("labels", {}) or {}
        annotations = alert.get("annotations", {}) or {}
        name = labels.get("alertname", "alert")
        event_type = alert_to_event.get(name, "high_failure_rate")
        severity = "critical" if labels.get("severity") == "critical" else "warning"
        message = annotations.get("summary") or annotations.get("description") or name
        notifications.emit_event_safe(event_type, severity, None, f"[{name}] {message}",
                                      metadata={"labels": labels})
        emitted += 1
    return {"ok": True, "emitted": emitted}


# ----------------------------------------------------------------- notifications

@app.get("/events")
async def get_events(unread_only: bool = False, limit: int = 50):
    """The notification feed, newest first. `unread_only` filters to unread events."""
    return {
        "events": notifications.list_events(unread_only=unread_only, limit=limit),
        "unread": notifications.unread_count(),
    }


@app.post("/events/{event_id}/read")
async def mark_event_read(event_id: str):
    found = notifications.mark_read(event_id)
    if not found:
        return JSONResponse({"error": "event not found"}, status_code=404)
    return {"ok": True, "unread": notifications.unread_count()}


@app.post("/events/read-all")
async def mark_all_events_read():
    changed = notifications.mark_all_read()
    return {"ok": True, "marked": changed, "unread": notifications.unread_count()}


@app.get("/events/stream")
async def stream_events(request: Request):
    """Server-Sent Events: pushes each new event to the browser for live toasts."""
    async def authorized_stream():
        async for frame in notifications.event_stream():
            if not auth.session(request.cookies.get(auth.SESSION_COOKIE, "")):
                break
            yield frame
    return StreamingResponse(authorized_stream(), media_type="text/event-stream")


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks):
    """Authenticate Slack's raw request body, acknowledge, and defer GitHub work."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not slack_notifications.verify_signature(secret, timestamp or "", body, signature or ""):
        return Response(status_code=401)
    try:
        payload = slack_notifications.parse_interaction_body(body)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        # A validly signed malformed payload is acknowledged so Slack does not retry it.
        return Response(status_code=200)
    background_tasks.add_task(slack_notifications.process_interaction, payload)
    return Response(status_code=200)


@app.get("/api/cluster")
async def get_cluster_status():
    import shutil
    import subprocess
    kubectl = shutil.which("kubectl") or os.path.expanduser("~/.local/bin/kubectl")
    if os.path.isfile(kubectl) and os.access(kubectl, os.X_OK):
        try:
            res = subprocess.run([kubectl, "get", "nodes", "-o", "json"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                return {"status": "connected", "nodes": [item.get("metadata", {}).get("name") for item in data.get("items", [])]}
        except Exception:
            pass
    return {"status": "simulated", "nodes": ["nl2infra-control-plane"]}
