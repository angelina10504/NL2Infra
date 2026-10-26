"""Phase 5 guards: the expanded benchmark dataset is well-formed and honest, and the charts
script renders from run logs."""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "eval"))

from contracts import RunState
from guardrails import check_guardrails

BENCH = os.path.join(ROOT, "benchmarks")
VALID_ROLES = {"junior_dev", "senior_dev", "platform_admin", "viewer", "developer", "admin"}


def _load(name):
    with open(os.path.join(BENCH, name), encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_dataset_sizes_match_the_spec():
    assert len(_load("correctness.jsonl")) == 30
    assert len(_load("adversarial.jsonl")) == 40


def test_correctness_entries_are_well_formed_and_unique():
    rows = _load("correctness.jsonl")
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    for r in rows:
        assert r["role"] in VALID_ROLES
        assert r["tier"] in {"easy", "medium", "hard"}
        assert isinstance(r["resources"], int) and r["resources"] >= 1
        assert r["expected"] == "pass" and r["prompt"].strip()


def test_adversarial_entries_are_well_formed_and_unique():
    rows = _load("adversarial.jsonl")
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    categories = {r.get("category") for r in rows}
    # The eight attack families the spec calls for are all represented.
    assert {"prompt_injection", "privilege_escalation", "privileged_container", "hostpath_mount",
            "latest_tag", "missing_limits", "wrong_apiversion", "delete_approved"} <= categories


def test_no_legitimate_request_is_falsely_refused():
    """All 30 correctness requests must pass the guardrails (role/env consistent, on-topic)."""
    refused = [r["id"] for r in _load("correctness.jsonl")
               if check_guardrails(RunState(request_id=r["id"], user_role=r["role"],
                                            user_prompt=r["prompt"], model="none"))]
    assert refused == [], f"false refusals: {refused}"


def test_input_layer_adversarials_are_refused_by_guardrails():
    """Injection / privilege-escalation / off-topic requests must be blocked up front."""
    rows = _load("adversarial.jsonl")
    input_layer = {"prompt_injection", "privilege_escalation", "off_topic"}
    for r in rows:
        if r.get("category") in input_layer:
            reason = check_guardrails(RunState(request_id=r["id"], user_role=r["role"],
                                               user_prompt=r["prompt"], model="none"))
            assert reason, f"{r['id']} ({r['category']}) should be refused by the guardrails"


def test_charts_render_from_synthetic_logs(tmp_path):
    import charts

    def log(arm, rid, passed, violations, rounds):
        return {
            "arm": arm, "benchmark": {"id": rid, "tier": "easy", "repeat": 1},
            "summary": {
                "outcome": "scored",
                "first": {"safety_pass": False, "safety_violations": violations},
                "final": {"safety_pass": passed, "safety_violations": 0 if passed else violations},
                "fix_rounds": rounds, "runtime_seconds": 1.5, "tokens_in": 100, "tokens_out": 40,
            },
        }

    runs = tmp_path / "runs"
    runs.mkdir()
    for arm, passed, v, r in [("A", False, 2, 0), ("B", True, 0, 0), ("C", True, 1, 1), ("D", True, 0, 0)]:
        (runs / f"r1-{arm}.json").write_text(json.dumps(log(arm, "r1", passed, v, r)))

    out = tmp_path / "charts"
    written = charts.make_charts(str(runs), str(out))
    assert len(written) == 5
    assert all(os.path.exists(p) and os.path.getsize(p) > 0 for p in written)
