# Pipeline tracing

The FastAPI app enables OTLP over HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set at startup. Set it to the collector base URL (for example, http://collector:4318 on a shared container network). The exporter appends /v1/traces. To disable export, unset the endpoint and restart the process. Compose sends spans to http://collector:4318 by default. To disable export in Compose, run `NL2INFRA_OTLP_ENDPOINT= make up`. OTEL_SERVICE_NAME defaults to nl2infra. OTEL_TRACES_SAMPLER selects the SDK sampler. NL2INFRA_ENVIRONMENT defaults to local.

Planning ends its trace when the graph pauses for human approval. Resuming generation starts a new root with the same request ID and a span link to the planning root when its context is available. Time spent awaiting a person is excluded from processing time.

| Span | Recorded information |
| --- | --- |
| nl2infra.run | Request ID, role, namespace, outcome and, when known, workload type and experiment arm |
| nl2infra.stage.NAME | Outcome and elapsed time for guardrails, profiler, planning, approval, generation, validation, fix, pr and deploy when executed |
| nl2infra.gauntlet.CHECKER | Outcome (pass, fail or crash), finding count and up to 20 sorted rule IDs |
| nl2infra.fix.round | Round number, incoming violation count and number of files sent when available |
| nl2infra.llm.call | Operation, model, attempt count and token counts when reported |
| nl2infra.llm.rate_limit_wait | Attempt number and wait in seconds |
| http.request | HTTP method and response status when available |

The exporter excludes request and prompt text, model output, manifests, file contents, credentials, cookies and query strings. Token counts are numeric usage totals. Exception messages are omitted. Metrics scrapes and the event stream are excluded from HTTP tracing.

Five paired mock runs produced identical outcomes, round counts, generated files and validation results. Median duration was 36.4 ms with tracing disabled and 37.3 ms with an in-memory exporter. These timings cover the local fixture. They do not measure collector delivery or production latency.

## Local evaluation

Run `make image PROVIDER=podman` and `make up`. The make target prepares a static HTTP health probe from the pinned Prometheus image. The Collector accepts traces on the internal Compose network, forwards them to Jaeger, and derives duration histograms. Prometheus scrapes the histogram endpoint every ten seconds. Jaeger uses in-memory storage for the demo, so restarting Jaeger clears its traces.

Open the app at http://localhost:8000, sign in, start a mock request, review its plan and approve it. In Grafana at http://localhost:3000, open **NL2Infra Trace Latency**. Each panel shows cumulative mean span duration in seconds grouped by stage or checker. Use **Explore**, select **Jaeger**, choose the **nl2infra** service and open a run trace to inspect timings for a single request. The planning and generation roots share a request ID.

During the final local check, authenticated mock request `9552baa83bca4d9ea69e8d4112e60e86` reached approval and then passed. Grafana returned the planning trace `6b3b47465643a167c6b274b301e5345e` and generation trace `8af819fa839e085de3140513aa9ec8e8`. The generation root carried a FOLLOWS_FROM reference to the planning root. The stage dashboard query returned nine series. The four checker series came from a separate run of the real validators against a mock-generated manifest fixture.
