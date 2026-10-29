"""Fit-benchmark tests: dataset shape and the pure scoring/CSV/summary helpers (no LLM)."""
import csv
import importlib.util
import os

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location("fit_benchmark", os.path.join(ROOT, "benchmarks", "fit_benchmark.py"))
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)


# ---------------------------------------------------------------- dataset

def test_dataset_is_six_types_by_three_phrasings():
    data = fb.load_dataset()
    assert len(data) == 18
    from collections import Counter
    counts = Counter(e["workload_type"] for e in data)
    assert set(counts) == {"web_app", "api", "database", "cron_job", "queue_worker", "ml_service"}
    assert all(c == 3 for c in counts.values())
    assert all(e["id"] and e["prompt"] and e["role"] for e in data)


# ---------------------------------------------------------------- scoring

GOOD_DB = """
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: pg, namespace: dev}
spec:
  volumeClaimTemplates: [{metadata: {name: data}}]
  template:
    spec:
      containers:
        - name: pg
          image: postgres:16
          readinessProbe: {exec: {command: [pg_isready]}}
          resources: {limits: {cpu: "1", memory: 512Mi}}
"""

WRONG_DB = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: pg, namespace: dev}
spec:
  template:
    spec:
      containers: [{name: pg, image: postgres:16}]
"""


def test_correct_kind_detects_the_defining_kind():
    assert fb.correct_kind({"d.yaml": GOOD_DB}, "database") is True
    assert fb.correct_kind({"d.yaml": WRONG_DB}, "database") is False   # Deployment, not StatefulSet


def test_musthave_coverage_is_full_for_a_good_manifest_and_low_for_a_bare_one():
    assert fb.musthave_coverage({"d.yaml": GOOD_DB}, "database") == 1.0
    assert fb.musthave_coverage({"d.yaml": WRONG_DB}, "database") < 1.0


def test_score_row_has_all_fields_and_handles_empty_files():
    entry = {"id": "db-1", "workload_type": "database", "role": "developer", "prompt": "x"}
    row = fb.score_row(entry, "D", 1, {"d.yaml": GOOD_DB}, checkov_violations=0, fix_rounds=1, seconds=3.2, status="passed")
    assert set(row) == set(fb.FIELDS)
    assert row["correct_kind"] is True and row["musthave_coverage"] == 1.0
    empty = fb.score_row(entry, "A", 1, {}, checkov_violations=-1, fix_rounds=0, seconds=1.0, status="error")
    assert empty["correct_kind"] is False and empty["musthave_coverage"] == 0.0


# ---------------------------------------------------------------- csv + summary

def _rows():
    e_db = {"id": "db-1", "workload_type": "database", "role": "developer", "prompt": "x"}
    e_web = {"id": "web-1", "workload_type": "web_app", "role": "developer", "prompt": "y"}
    return [
        fb.score_row(e_db, "C", 1, {"d.yaml": WRONG_DB}, 3, 5, 9.0, "escalated"),   # pipeline, project-aware off
        fb.score_row(e_db, "D", 1, {"d.yaml": GOOD_DB}, 0, 1, 7.0, "passed"),       # project-aware on
        fb.score_row(e_web, "D", 1, {"d.yaml": GOOD_DB}, 0, 0, 5.0, "passed"),
    ]


def test_summary_shows_project_aware_doing_better():
    summary = fb.summarize(_rows())
    assert summary["C"]["kind_correct_pct"] == 0.0      # database rendered as a Deployment
    assert summary["D"]["kind_correct_pct"] == 50.0     # the db one is correct; the web one is a StatefulSet (wrong kind)
    assert summary["D"]["avg_fix_rounds"] < summary["C"]["avg_fix_rounds"]
    text = fb.format_summary(summary)
    assert "Kind OK %" in text and "D p-aware" in text


def test_write_csv_round_trips(tmp_path):
    path = str(tmp_path / "out.csv")
    fb.write_csv(_rows(), path)
    with open(path, newline="", encoding="utf-8") as f:
        back = list(csv.DictReader(f))
    assert len(back) == 3 and list(back[0]) == fb.FIELDS


def test_dry_count_does_not_call_the_model(capsys):
    fb.main(["--dry-count", "--arm", "all", "--repeats", "2"])
    out = capsys.readouterr().out
    assert "18 requests x 2" in out and "call budget" in out
