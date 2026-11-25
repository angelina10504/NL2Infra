"""Optional OTLP tracing. The application owns this provider and its export queue."""
import atexit
import logging
import os
import threading
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Status, StatusCode

_logger = logging.getLogger(__name__)
_provider = None
_lock = threading.Lock()
_warned = False


def _warn_once():
    global _warned
    if not _warned:
        _warned = True
        _logger.warning("Trace export unavailable; application processing continues.")


def init_tracing():
    global _provider
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint or _provider is not None:
        return _provider
    with _lock:
        if _provider is None:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=1)
                # The transport logs URLs and retry errors. Report a single generic failure instead.
                logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter").disabled = True
                _provider = _make_provider(exporter)
            except Exception:
                _warn_once()
    return _provider


def _make_provider(exporter):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
    from storage import git_commit

    class QuietExporter(SpanExporter):
        def export(self, spans):
            try:
                result = exporter.export(spans)
                if result == SpanExportResult.SUCCESS:
                    return result
            except Exception:
                pass
            _warn_once()
            return SpanExportResult.FAILURE

        def shutdown(self):
            exporter.shutdown()

    class PrivateBatchProcessor(BatchSpanProcessor):
        def on_end(self, ended):
            # FastAPI may attach raw paths, queries, exception messages and header values.
            # Only the method, status and timing of HTTP requests may enter the export queue.
            if ended.instrumentation_scope.name != "nl2infra":
                attrs = ended.attributes or {}
                ended = ReadableSpan(
                    name="http.request", context=ended.context, parent=ended.parent,
                    resource=ended.resource, kind=ended.kind, start_time=ended.start_time,
                    end_time=ended.end_time, instrumentation_scope=ended.instrumentation_scope,
                    status=Status(ended.status.status_code),
                    attributes={key: attrs[key] for key in (
                        "http.method", "http.request.method", "http.status_code",
                        "http.response.status_code") if key in attrs},
                )
            super().on_end(ended)

    provider = TracerProvider(resource=Resource({
        "service.name": os.getenv("OTEL_SERVICE_NAME") or "nl2infra",
        "service.version": git_commit() or "unknown",
        "deployment.environment": os.getenv("NL2INFRA_ENVIRONMENT") or "local",
    }), shutdown_on_exit=False)
    provider.add_span_processor(PrivateBatchProcessor(QuietExporter(), max_queue_size=512,
                                max_export_batch_size=64, schedule_delay_millis=500))
    return provider


def shutdown_tracing():
    global _provider
    with _lock:
        provider, _provider = _provider, None
    if provider is not None:
        worker = threading.Thread(target=provider.shutdown, daemon=True)
        worker.start()
        worker.join(timeout=2)


atexit.register(shutdown_tracing)


def instrument_app(app):
    provider = init_tracing()
    if provider is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls=r"/metrics(?:\?|$),/events/stream(?:\?|$)",
        exclude_spans=["receive", "send"],
    )


@contextmanager
def span(name, *, root=False, previous=None, attributes=None):
    if _provider is None:
        yield trace.INVALID_SPAN
        return
    try:
        active = _provider.get_tracer("nl2infra").start_span(
            "nl2infra." + name, context=Context() if root else None,
            links=[trace.Link(previous)] if previous and previous.is_valid else (),
            attributes=attributes,
        )
    except Exception:
        _warn_once()
        yield trace.INVALID_SPAN
        return
    with trace.use_span(active, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield active
        except Exception as error:
            # Error messages can contain prompts, manifests or provider URLs.
            from langgraph.errors import GraphBubbleUp
            if not isinstance(error, GraphBubbleUp):
                active.set_status(StatusCode.ERROR)
            raise
