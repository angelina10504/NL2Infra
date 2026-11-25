# PHASE-4 — OpenTelemetry tracing in the app

Goal: every pipeline run produces **one trace** showing where its time goes: each stage,
each Gauntlet checker, each LLM call, each fix round, and each rate-limit wait. The traces
are exported over OTLP when a collector is configured, and are a **complete no-op** when it
isn't.

The Collector, Jaeger and Grafana panels come in Phase 5. In this phase spans are proven
with tests and an in-memory exporter.

**Hard rule 1 applies with full force.** Tracing wraps behaviour and never changes it: no
edits to Gauntlet decisions, policies, prompts, the benchmark runner, evaluation or datasets.
Step 6 proves that nothing changed.

---

## Step 0 — First live run through the container (carried over from Phase 3)

Phase 3 couldn't verify a real pipeline run in the container, because sign-in was required.
Do this before writing any tracing code:
1. Print `MOCK_MODE` **as the running app container sees it** (`podman exec … printenv MOCK_MODE`;
   it's not a secret). If it isn't `true`, stop and tell me. A run must not call Groq.
2. Find out whether GitHub sign-in works at `http://localhost:8000`: what callback URL the app
   uses (`NL2INFRA_AUTH_BASE_URL`, key name and whether it's localhost or a tunnel; print the
   value only if it's a plain URL with no token). Tell me exactly what to open and click.
3. **I'll sign in and start one run from the UI.** Don't create sessions or bypass auth.
4. Then check and report:
   - the run completed, with Checkov, Conftest and kubectl **all** working under the read-only
     root filesystem (look for `TOOL_CRASH`)
   - the run log in the container's state volume
   - Prometheus counters that moved
   - whether Grafana panels now show data (API check is enough)

If a tool crashes on the read-only filesystem, stop and report. Don't loosen the hardening
without asking me.

## Step 1 — Map before changing

Report:
1. **The run lifecycle.** Is the pipeline one process run, or split by the human approval
   (plan → wait → generate…)? Where does the benchmark runner enter the pipeline? Spans must
   live inside the pipeline code, not the runner, so benchmark runs are traced too without
   editing `benchmarks/`.
2. **Every call to `timing.track_time`**, and what keys it uses. I want spans to reuse these
   names, so metrics, run-log timings and traces agree.
3. **Where each Gauntlet checker, each LLM call, each fix round and each 429 back-off happens**,
   by file and line.
4. **Which attributes are available at each point:** request id, role, namespace, workload type,
   arm, round, model, tokens in/out, violation count, rule ids, outcome.

## Step 2 — Dependencies

Add, pinned exactly, the current stable releases of:
- `opentelemetry-api`
- `opentelemetry-sdk`
- `opentelemetry-exporter-otlp-proto-http` (HTTP, so no gRPC wheel)
- `opentelemetry-instrumentation-fastapi`

Adding them **must not change any existing pin in `requirements.lock`**. If the resolver wants
to change one (for example `protobuf`), stop and report which pin and why.
**Ask me before installing into `.venv`.**

## Step 3 — `src/nl2infra/telemetry.py`

- `init_tracing()` reads the standard env vars (`OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_SERVICE_NAME`, `OTEL_TRACES_SAMPLER`).
  - **If no endpoint is set:** install nothing and return. The OTel API then hands out its
    built-in no-op tracer.
  - **If an endpoint is set:** install a `TracerProvider` with a `BatchSpanProcessor` and the
    OTLP HTTP exporter, with a short export timeout. Resource attributes: `service.name=nl2infra`,
    `service.version`=git commit (same source as the run logs, including `NL2INFRA_GIT_COMMIT`),
    and `deployment.environment` from an env var, defaulting to `local`.
- Export failures must never raise into the app, block a request, or flood the logs: one
  warning at most, then quiet.
- Shut down cleanly on app exit, with a timeout.
- A small helper (context manager or decorator) for opening spans, so the instrumentation
  in the pipeline stays one or two lines per place.
- Call `init_tracing()` once at FastAPI startup, and add `FastAPIInstrumentor`, excluding
  `/metrics` and `/events/stream` so scrapes and the SSE bell don't flood traces.

## Step 4 — Spans

Span names use the `nl2infra.` prefix and reuse the `track_time` keys where they exist. If
extending `track_time` itself to open a span is the cleanest hook, do that.

| Span | Where | Key attributes |
|---|---|---|
| `nl2infra.run` (root of the pipeline part) | pipeline entry | `nl2infra.request_id`, `nl2infra.role`, `nl2infra.namespace`, `nl2infra.workload_type`, `nl2infra.arm` (if known), `nl2infra.outcome` |
| `nl2infra.stage.<name>` | each stage: guardrails, plan, generate, gauntlet, fix, pr | stage outcome |
| `nl2infra.gauntlet.<checker>` | each checker | `nl2infra.violations.count`, `nl2infra.rule_ids` (max 20, sorted), `nl2infra.check.outcome` = pass / fail / crash |
| `nl2infra.fix.round` | each fix round | `nl2infra.fix.round`, files sent, violations before |
| `nl2infra.llm.call` | each model call | OTel GenAI conventions: `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, plus `nl2infra.llm.attempts` |
| `nl2infra.llm.rate_limit_wait` | each 429 back-off | wait seconds, attempt number |

The plan and the generation are split by a human approval, which can take minutes or hours.
Don't keep a span open across it. Use two traces that share `nl2infra.request_id`, and give
the second a **span link** to the first.

**Never put these in a span, attribute or event:** prompt text, model output, the user's
request text, YAML, file contents, tokens, keys, cookies or URLs with query strings. Set
`ERROR` status on exceptions and checker crashes.

## Step 5 — Tests (new file `tests/test_telemetry.py`)

1. **No-op.** With no endpoint set, `init_tracing()` installs no SDK provider, and a mock
   pipeline run makes no network call.
2. **Collector down.** With the endpoint set to a closed port (e.g. `http://127.0.0.1:9`), a mock
   run completes with the same result, and shutdown returns within the timeout. Measure and
   assert an upper bound on the extra time.
3. **Span tree.** Use an in-memory exporter and a mock run that reaches the Gauntlet and at
   least one fix round. Assert that the expected span names exist, the parent–child structure is
   right, the attributes are present, the checker outcomes match the run log, and the second
   trace links to the first.
4. **No leaks.** Set fake values for `GROQ_API_KEY`, `GITHUB_TOKEN` and a distinctive request text,
   run, and assert that none of them appears in any span name, attribute or event.

## Step 6 — Prove behaviour didn't change

1. The full suite must pass on the host and in the image. Expected: 437 plus the new tests, with
   0 failed and 0 skipped. Rego: 46/46.
2. **Same results with tracing on and off.** For the mock fixtures used by the tests (or 5
   pilot requests in `MOCK_MODE`), run the pipeline with tracing off and with tracing on
   (in-memory exporter). Compare Gauntlet results, violations, rounds and final outcome:
   they must be identical. Show the comparison.
3. **Overhead.** On those same runs, report the median duration with tracing off and on.
4. `git diff --stat` must show no change under `policies/`, `benchmarks/`, `eval/` or any
   prompt file. Show it.

## Step 7 — Container

Rebuild the image with `make image PROVIDER=podman`, then run `make image-test PROVIDER=podman`,
which must pass. In `compose.yaml` set `OTEL_SERVICE_NAME=nl2infra` on the app, but **leave
`OTEL_EXPORTER_OTLP_ENDPOINT` unset**: tracing stays a no-op until Phase 5 adds the Collector.
Then `make up` and confirm everything is healthy.

## Step 8 — Checkpoint

1. Write `docs/tracing.md`, covering:
   - span names and attributes, as a table
   - what is never recorded
   - the two-trace design across the approval
   - how to turn tracing on and off
   - the overhead you measured
2. Propose a commit with `telemetry.py`, the instrumented files, tests, the lock and requirements
   files, `compose.yaml`, the docs, `AGENTS.md` and `PHASE-4.md`. **Ask me before committing and
   before pushing.**
3. Give the AGENTS.md §7 report and stop.
