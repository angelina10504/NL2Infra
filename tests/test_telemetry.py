"""Exercise the normal approval, retry and checker paths without a provider or a cluster."""
import json
import socket
import statistics
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import telemetry
from contracts import Violation
from mocks import MockGitOps, MockLLMService
from pipeline import Pipeline
from tests.test_llm_service import HttpError, service
from validators import Validators

REQUEST = "Deploy an nginx service in dev for private-request-761."
SECRET = "secret-never-export-761"


@pytest.fixture(autouse=True)
def isolated_tracing(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_warned", False)
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    yield
    telemetry.shutdown_tracing()


def memory_export(monkeypatch):
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(telemetry, "_provider", telemetry._make_provider(exporter))
    return exporter


def approved_run(tmp_path, monkeypatch):
    mock = MockLLMService()
    plan = mock.plan(REQUEST, "junior_dev")
    files = mock.generate(plan)
    deployment, svc = files.values()
    model = service(HttpError(429), plan.model_dump_json(), deployment, svc, deployment)
    validator = Validators()
    monkeypatch.setattr(validator, "tool_versions", lambda: {})
    findings = iter([[Violation(tool="checkov", rule_id="CKV_K8S_1", file=next(iter(files)),
                                message=SECRET, severity="HIGH")], []])
    monkeypatch.setattr(validator, "_run_checkov", lambda *a, **k: next(findings))
    monkeypatch.setattr(validator, "_run_conftest", lambda *a: [])
    monkeypatch.setattr(validator, "_run_kubectl_dry_run", lambda *a: [])
    pipeline = Pipeline(model, validator, MockGitOps(), run_dir=str(tmp_path))
    first = pipeline.start(REQUEST, "junior_dev", bypass_guardrails=True, arm="C")
    assert pipeline.awaiting_approval(first.request_id)
    result = pipeline.resume(first.request_id, approved=True)
    assert result.status == "passed" and result.iterations == 1
    return result


def test_no_endpoint_no_network(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected network connection")
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    assert telemetry.init_tracing() is None
    approved_run(tmp_path, monkeypatch)
    assert telemetry._provider is None


def test_down_collector_does_not_block_or_change_result(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    before = time.monotonic()
    approved_run(tmp_path, monkeypatch)
    assert time.monotonic() - before < 2
    before = time.monotonic()
    telemetry.shutdown_tracing()
    assert time.monotonic() - before < 2.5
    assert sum("Trace export unavailable" in r.message for r in caplog.records) <= 1


def test_linked_spans_and_privacy(tmp_path, monkeypatch):
    exporter = memory_export(monkeypatch)
    result = approved_run(tmp_path, monkeypatch)
    telemetry._provider.force_flush()
    spans = exporter.get_finished_spans()
    def named(name):
        return [s for s in spans if s.name == "nl2infra." + name]

    plan, generation = named("run")
    assert plan.context.trace_id != generation.context.trace_id
    assert generation.links[0].context == plan.context
    assert plan.attributes["nl2infra.outcome"] == "awaiting_approval"
    assert generation.attributes["nl2infra.outcome"] == "passed"
    assert generation.attributes["nl2infra.request_id"] == result.request_id
    for stage in named("stage.generation") + named("stage.validation"):
        assert stage.parent == generation.context
    check_spans = named("gauntlet.checkov")
    assert [s.attributes["nl2infra.check.outcome"] for s in check_spans] == ["fail", "pass"]
    for check, log, parent in zip(check_spans, result.rounds, named("stage.validation"), strict=True):
        assert check.parent == parent.context
        assert check.attributes["nl2infra.violations.count"] == len(log["violations"])
        assert list(check.attributes["nl2infra.rule_ids"]) == sorted({v["rule_id"] for v in log["violations"]})
    for checker in ("plan", "opa", "dry-run"):
        assert len(named("gauntlet." + checker)) == 2
    repair, = named("fix.round")
    assert repair.parent == named("stage.fix")[0].context
    assert repair.attributes["nl2infra.fix.files"] == 1
    calls = named("llm.call")
    wait, = named("llm.rate_limit_wait")
    assert wait.parent == calls[0].context
    assert calls[0].attributes["nl2infra.llm.attempts"] == 2
    assert calls[0].attributes["gen_ai.usage.input_tokens"] == 11
    assert calls[-1].parent == repair.context
    encoded = "".join(s.to_json() for s in spans)
    for forbidden in (SECRET, "private-request-761", "apiVersion:", "kind: Deployment"):
        assert forbidden not in encoded


def test_http_metadata_excludes_query_cookies_exceptions_and_scrapes(monkeypatch):
    exporter = memory_export(monkeypatch)
    app = FastAPI()
    telemetry.instrument_app(app)

    @app.get("/demo")
    def demo():
        raise ValueError(SECRET)

    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("session", SECRET)
    assert client.get("/demo?token=" + SECRET).status_code == 500
    client.get("/metrics")
    client.get("/events/stream")
    telemetry._provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].name == "http.request"
    assert SECRET not in spans[0].to_json()


def test_paired_benchmark_unchanged(tmp_path, monkeypatch):
    times = {False: [], True: []}
    for i in range(5):
        outcomes = []
        for enabled in ((False, True) if i % 2 == 0 else (True, False)):
            if enabled:
                memory_export(monkeypatch)
            start = time.perf_counter()
            result = approved_run(tmp_path, monkeypatch)
            times[enabled].append(time.perf_counter() - start)
            outcomes.append((result.status, result.iterations, result.files, result.violations,
                             [(r["round"], r["violations"], r["files"]) for r in result.rounds]))
            telemetry.shutdown_tracing()
        assert outcomes[0] == outcomes[1]
    print("Five paired mock runs, median seconds:", json.dumps({
        "off": statistics.median(times[False]), "on": statistics.median(times[True]),
    }))
