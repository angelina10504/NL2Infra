import os
import sys
import uuid
import json
import logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure src and root are in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from contracts import RunState, Plan, Resource, Violation
from guardrails import check_guardrails
from pipeline import Pipeline
from storage import load_runs
import notifications
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from fastapi.responses import Response

logger = logging.getLogger(__name__)

app = FastAPI(title="NL2Infra Enterprise Studio", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "index.html")


class RunRequest(BaseModel):
    prompt: str
    role: str = "junior_dev"
    model: Optional[str] = None
    use_gauntlet: bool = True


class ApproveRequest(BaseModel):
    request_id: str
    approved: bool


class GuardrailCompareRequest(BaseModel):
    question: str
    role: str = "junior_dev"


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>NL2Infra Studio template not found</h1>", status_code=404)


_pipeline = None


def get_pipeline() -> Pipeline:
    # One pipeline per server process: it holds the runs that are waiting for approval.
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline(auto_approve=False)
    return _pipeline


def _response(state: RunState, awaiting_approval: bool) -> dict:
    return {
        "request_id": state.request_id,
        "status": "awaiting_approval" if awaiting_approval else state.status,
        "plan": state.plan.model_dump() if state.plan else None,
        "files": state.files or {},
        "violations": [v.model_dump() for v in state.violations],
        "rounds": state.rounds,
        "iterations": state.iterations,
        "pr_url": state.pr_url,
        "pr_error": state.pr_error,
        "metrics": state.metrics,
        "rejection_reason": state.rejection_reason
    }


@app.post("/api/run")
@app.post("/ask")
def run_pipeline(req: RunRequest):
    """Guardrails and planning. Returns the plan with status 'awaiting_approval'; nothing is generated yet."""
    pipeline = get_pipeline()
    state = pipeline.start(req.prompt, req.role)
    return _response(state, pipeline.awaiting_approval(state.request_id))


@app.post("/api/approve")
def approve_plan(req: ApproveRequest):
    """The user's decision on the plan. On approval the run continues to the end."""
    pipeline = get_pipeline()
    try:
        state = pipeline.resume(req.request_id, approved=req.approved)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=409)
    return _response(state, False)


@app.post("/api/guardrail-compare")
@app.post("/ask/guardrail-compare")
async def guardrail_compare(req: GuardrailCompareRequest):
    question_lower = req.question.lower()

    # Check guardrails
    state = RunState(
        request_id="eval-" + str(uuid.uuid4())[:6],
        user_role=req.role,
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
    elif "production" in question_lower and req.role == "junior_dev":
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


@app.get("/api/runs")
async def get_runs():
    runs = load_runs()
    return [r.model_dump() for r in runs[-20:]]


# ------------------------------------------------------------------- metrics

@app.get("/metrics")
async def metrics_endpoint():
    """Prometheus scrape target."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/alerts/webhook")
async def alerts_webhook(payload: dict):
    """Alertmanager posts here; each firing alert becomes a dashboard notification so
    system-health alerts also show up in the bell."""
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
async def stream_events():
    """Server-Sent Events: pushes each new event to the browser for live toasts."""
    return StreamingResponse(notifications.event_stream(), media_type="text/event-stream")


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
