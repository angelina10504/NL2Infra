"""Prometheus metrics for NL2Infra.

The pipeline calls `record_*` after each run; the FastAPI app exposes the registry at
`/metrics`. Everything is in-process and local — no push gateway, no cloud.

Metrics are module-level singletons (the Prometheus client keeps one global registry), so
importing this module more than once is safe.
"""
from typing import List, Optional

from prometheus_client import Counter, Gauge, Histogram

# How many requests reach each final status, split by target environment.
requests_total = Counter(
    "nl2infra_requests_total", "Completed requests by final status and environment", ["status", "env"]
)

# Requests the guardrails blocked, by reason code.
blocked_requests_total = Counter(
    "nl2infra_blocked_requests_total", "Requests blocked by the guardrails", ["reason"]
)

# How many fix-loop rounds a run needed (0 = passed on the first draft).
fix_loop_rounds = Histogram(
    "nl2infra_fix_loop_rounds", "Fix-loop rounds per run", buckets=(0, 1, 2, 3, 4, 5)
)

# Every violation the Gauntlet found, by tool.
violations_total = Counter(
    "nl2infra_violations_total", "Violations found by the Gauntlet", ["tool"]
)

# Wall-clock time for a whole run.
pipeline_duration_seconds = Histogram(
    "nl2infra_pipeline_duration_seconds", "End-to-end pipeline duration (seconds)",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

# LLM tokens consumed, in and out.
llm_tokens_total = Counter(
    "nl2infra_llm_tokens_total", "LLM tokens used", ["direction"]
)

# Per-call LLM latency.
llm_latency_seconds = Histogram(
    "nl2infra_llm_latency_seconds", "LLM call latency (seconds)",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30),
)

# Runs currently waiting for a human to approve their plan.
pending_approvals = Gauge(
    "nl2infra_pending_approvals", "Runs waiting for plan approval"
)


def record_blocked(reason_code: str) -> None:
    blocked_requests_total.labels(reason=reason_code or "unknown").inc()


def record_round_violations(violations) -> None:
    """Count the violations from one Gauntlet round, by tool."""
    for v in violations or []:
        tool = v.get("tool") if isinstance(v, dict) else getattr(v, "tool", None)
        violations_total.labels(tool=tool or "unknown").inc()


def record_run(status: str, env: str, iterations: int, duration: Optional[float],
               tokens_in: Optional[int], tokens_out: Optional[int],
               llm_call_seconds: Optional[List[float]] = None) -> None:
    """Record a run that reached a final status."""
    requests_total.labels(status=status or "unknown", env=env or "unknown").inc()
    fix_loop_rounds.observe(max(int(iterations or 0), 0))
    if duration is not None:
        pipeline_duration_seconds.observe(float(duration))
    if tokens_in:
        llm_tokens_total.labels(direction="in").inc(int(tokens_in))
    if tokens_out:
        llm_tokens_total.labels(direction="out").inc(int(tokens_out))
    for seconds in llm_call_seconds or []:
        if seconds is not None:
            llm_latency_seconds.observe(float(seconds))
