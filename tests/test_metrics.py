"""Prometheus metrics tests: the /metrics endpoint, the record_* helpers, the gauge wired
through the pipeline, and the Alertmanager webhook that turns alerts into dashboard events."""
from prometheus_client import REGISTRY

import metrics
import notifications
from fastapi.testclient import TestClient


def val(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


SERIES = [
    "nl2infra_requests_total", "nl2infra_blocked_requests_total", "nl2infra_violations_total",
    "nl2infra_pipeline_duration_seconds_bucket", "nl2infra_llm_tokens_total",
    "nl2infra_llm_latency_seconds_bucket", "nl2infra_fix_loop_rounds_bucket", "nl2infra_pending_approvals",
]


def test_metrics_endpoint_exposes_all_series():
    import server
    body = TestClient(server.app).get("/metrics")
    assert body.status_code == 200
    for name in SERIES:
        assert name in body.text


def test_record_run_increments_requests_and_tokens():
    before = val("nl2infra_requests_total", {"status": "passed", "env": "dev"})
    before_in = val("nl2infra_llm_tokens_total", {"direction": "in"})
    metrics.record_run("passed", "dev", 1, 2.0, tokens_in=123, tokens_out=45, llm_call_seconds=[0.5])
    assert val("nl2infra_requests_total", {"status": "passed", "env": "dev"}) == before + 1
    assert val("nl2infra_llm_tokens_total", {"direction": "in"}) == before_in + 123


def test_record_blocked_increments_by_reason():
    before = val("nl2infra_blocked_requests_total", {"reason": "injection_attempt"})
    metrics.record_blocked("injection_attempt")
    assert val("nl2infra_blocked_requests_total", {"reason": "injection_attempt"}) == before + 1


def test_record_round_violations_counts_by_tool():
    before = val("nl2infra_violations_total", {"tool": "opa"})
    metrics.record_round_violations([{"tool": "opa"}, {"tool": "checkov"}, {"tool": "opa"}])
    assert val("nl2infra_violations_total", {"tool": "opa"}) == before + 2


def test_pipeline_pauses_bump_pending_approvals_gauge(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    from pipeline import Pipeline
    before = val("nl2infra_pending_approvals")
    p = Pipeline(auto_approve=False, run_dir=str(tmp_path))
    p.start("Deploy nginx to dev", "developer", request_id="m1")
    assert val("nl2infra_pending_approvals") == before + 1
    p.resume("m1", approved=True)
    assert val("nl2infra_pending_approvals") == before  # released on completion


def test_alert_webhook_emits_dashboard_event():
    import server
    client = TestClient(server.app)
    payload = {"alerts": [{
        "status": "firing",
        "labels": {"alertname": "LowSuccessRate", "severity": "critical"},
        "annotations": {"summary": "success rate below 80%"},
    }]}
    res = client.post("/alerts/webhook", json=payload).json()
    assert res["emitted"] == 1
    assert any(e["type"] == "high_failure_rate" and e["severity"] == "critical"
               for e in notifications.list_events())
