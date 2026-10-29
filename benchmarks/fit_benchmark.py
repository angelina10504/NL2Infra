#!/usr/bin/env python3
"""Project-Aware Generation benchmark.

Six project types x three phrasings, four arms, same model and same Gauntlet:

  A = one plain LLM call, no rules
  B = one LLM call with the rule list in the prompt
  C = the current pipeline (plan -> generate -> Gauntlet -> fix loop), project-aware OFF
  D = the pipeline with Project-Aware Generation ON (profiler + type-fit grounding + fit checks)

For every run it records: did the correct Kubernetes kind appear (yes/no), must-have coverage
(% of the type's must-haves present), Checkov violations, fix rounds used, and wall-clock time.
Results are written to results/fit_benchmark.csv with a short per-arm summary printed.

Nothing is sent to the model without --yes. Use --dry-count to see the call budget.
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import time
from typing import Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "nl2infra"))
sys.path.insert(0, ROOT)

import fit  # noqa: E402
from contracts import ProjectProfile, WorkloadType  # noqa: E402

ARMS = ("A", "B", "C", "D")
DATASET = os.path.join(ROOT, "benchmarks", "fit_dataset.jsonl")
OUT_CSV = os.path.join(ROOT, "results", "fit_benchmark.csv")
FIELDS = ["id", "arm", "repeat", "workload_type", "correct_kind", "musthave_coverage",
          "checkov_violations", "fix_rounds", "seconds", "status"]

ARM_A_PROMPT = (
    "Write the Kubernetes YAML manifests for this request.\n\nRequest: {prompt}\n\n"
    "Reply with the YAML only, as one multi-document YAML (documents separated by ---). No explanation.\n"
)
ARM_B_PROMPT = (
    "Write the Kubernetes YAML manifests for this request.\n\nRequest: {prompt}\n\n"
    "The manifests will be checked against every rule below. All of them must pass.\n{rules}\n\n"
    "Reply with the YAML only, as one multi-document YAML (documents separated by ---). No explanation.\n"
)


# ----------------------------------------------------------------- pure scoring (no LLM)

def load_dataset(path: str = DATASET) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def primary_kind(workload_type) -> str:
    """The defining kind for a type (the first in requirements/<type>.yaml)."""
    kinds = fit.expected_kinds(workload_type)
    return kinds[0] if kinds else ""


def correct_kind(files: Dict[str, str], workload_type) -> bool:
    """True if the type's defining Kubernetes kind appears in the generated YAML."""
    from validators import parse_files
    resources, _, _ = parse_files(files)
    kinds = {r.kind.lower() for r in resources}
    return primary_kind(workload_type).lower() in kinds


def musthave_coverage(files: Dict[str, str], workload_type) -> float:
    """Fraction of the type's must-haves that are present (1.0 = all present)."""
    profile = ProjectProfile(workload_type=workload_type, app_name="bench")
    required = fit.required_must_haves(profile)
    if not required:
        return 1.0
    missing_ids = {v.rule_id.replace("FIT_MUSTHAVE_", "").lower() for v in fit.fit_conformance(files, profile)}
    present = sum(1 for m in required if m["id"].lower() not in missing_ids)
    return round(present / len(required), 3)


def score_row(entry: dict, arm: str, repeat: int, files: Dict[str, str],
              checkov_violations: int, fix_rounds: int, seconds: float, status: str) -> dict:
    """One CSV row: the metrics for a single (request, arm, repeat)."""
    wt = entry["workload_type"]
    return {
        "id": entry["id"], "arm": arm, "repeat": repeat, "workload_type": wt,
        "correct_kind": correct_kind(files, wt) if files else False,
        "musthave_coverage": musthave_coverage(files, wt) if files else 0.0,
        "checkov_violations": checkov_violations, "fix_rounds": fix_rounds,
        "seconds": round(seconds, 2), "status": status,
    }


def write_csv(rows: List[dict], path: str = OUT_CSV) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: List[dict]) -> Dict[str, dict]:
    """Per-arm averages across all runs of that arm."""
    out: Dict[str, dict] = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        if not arm_rows:
            continue
        n = len(arm_rows)
        out[arm] = {
            "runs": n,
            "kind_correct_pct": round(100 * sum(1 for r in arm_rows if r["correct_kind"]) / n, 1),
            "musthave_coverage_pct": round(100 * sum(float(r["musthave_coverage"]) for r in arm_rows) / n, 1),
            "avg_checkov": round(sum(int(r["checkov_violations"]) for r in arm_rows) / n, 2),
            "avg_fix_rounds": round(sum(int(r["fix_rounds"]) for r in arm_rows) / n, 2),
            "avg_seconds": round(sum(float(r["seconds"]) for r in arm_rows) / n, 2),
        }
    return out


def format_summary(summary: Dict[str, dict]) -> str:
    header = f"{'Arm':<4}{'Runs':>6}{'Kind OK %':>11}{'Must-have %':>13}{'Checkov':>9}{'Fix rounds':>12}{'Seconds':>9}"
    lines = [header, "-" * len(header)]
    labels = {"A": "A plain", "B": "B rules", "C": "C pipe", "D": "D p-aware"}
    for arm in ARMS:
        if arm not in summary:
            continue
        s = summary[arm]
        lines.append(f"{labels[arm]:<4}{s['runs']:>6}{s['kind_correct_pct']:>11}{s['musthave_coverage_pct']:>13}"
                     f"{s['avg_checkov']:>9}{s['avg_fix_rounds']:>12}{s['avg_seconds']:>9}")
    return "\n".join(lines)


# ----------------------------------------------------------------- LLM arms (need --yes)

def _checkov_count(files: Dict[str, str], role: str) -> int:
    """Checkov failures for a set of files (0 if none; -1 if Checkov could not run)."""
    from validators import Validators, parse_files
    v = Validators()
    _, _, scannable = parse_files(files)
    if not scannable:
        return -1
    with tempfile.TemporaryDirectory(prefix="fitbench_") as d:
        names = {}
        for name in scannable:
            safe = os.path.basename(name) if name.endswith((".yaml", ".yml")) else f"{name}.yaml"
            names[safe] = name
            with open(os.path.join(d, safe), "w", encoding="utf-8") as f:
                f.write(files[name])
        viols = v._run_checkov(d, names, expect_resources=True)
    if any(x.rule_id == "TOOL_CRASH" for x in viols):
        return -1
    return sum(1 for x in viols if x.tool == "checkov")


def run_plain_arm(entry: dict, arm: str, llm) -> dict:
    """Arms A and B: one LLM call, score the raw output."""
    from llm_service import _extract_yaml
    from rules import rules_for_role
    if arm == "A":
        prompt = ARM_A_PROMPT.format(prompt=entry["prompt"])
    else:
        prompt = ARM_B_PROMPT.format(prompt=entry["prompt"], rules=rules_for_role(entry["role"]))
    started = time.time()
    try:
        files = {"manifests.yaml": _extract_yaml(llm._call(prompt, "generate"))}
        status = "ok"
    except Exception as error:
        files, status = {}, f"error: {type(error).__name__}"
    seconds = time.time() - started
    checkov = _checkov_count(files, entry["role"]) if files else -1
    return {"files": files, "checkov": checkov, "fix_rounds": 0, "seconds": seconds, "status": status}


def run_pipeline_arm(entry: dict, arm: str) -> dict:
    """Arms C and D: the full pipeline, project-aware off (C) or on (D)."""
    from pipeline import Pipeline
    pipeline = Pipeline(auto_approve=True)
    started = time.time()
    state = pipeline.start(entry["prompt"], entry["role"], project_aware=(arm == "D"))
    seconds = time.time() - started
    files = state.files or {}
    last = state.rounds[-1]["violations"] if state.rounds else []
    checkov = sum(1 for v in last if v.get("tool") == "checkov")
    return {"files": files, "checkov": checkov, "fix_rounds": state.iterations,
            "seconds": seconds, "status": state.status}


def run(arms: List[str], repeats: int) -> List[dict]:
    from llm_service import LLMService
    dataset = load_dataset()
    llm = LLMService() if any(a in ("A", "B") for a in arms) else None
    rows = []
    for repeat in range(1, repeats + 1):
        for entry in dataset:
            for arm in arms:
                result = run_plain_arm(entry, arm, llm) if arm in ("A", "B") else run_pipeline_arm(entry, arm)
                rows.append(score_row(entry, arm, repeat, result["files"], result["checkov"],
                                      result["fix_rounds"], result["seconds"], result["status"]))
                print(f"  {entry['id']:<9} arm {arm} r{repeat}: kind_ok="
                      f"{correct_kind(result['files'], entry['workload_type']) if result['files'] else False} "
                      f"coverage={musthave_coverage(result['files'], entry['workload_type']) if result['files'] else 0.0} "
                      f"checkov={result['checkov']} fix={result['fix_rounds']} {result['status']}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Project-Aware Generation benchmark")
    parser.add_argument("--yes", action="store_true", help="actually call the model (needed for a real run)")
    parser.add_argument("--dry-count", action="store_true", help="print the call budget and exit")
    parser.add_argument("--arm", default="all", help="all, or a comma-separated subset of A,B,C,D")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--out", default=OUT_CSV)
    args = parser.parse_args(argv)

    arms = list(ARMS) if args.arm == "all" else [a.strip().upper() for a in args.arm.split(",")]
    dataset = load_dataset()

    if args.dry_count or not args.yes:
        # A/B: 1 call each. C/D: 1 plan call + 1 call per planned resource (+1 profile call for D).
        budget = len(dataset) * args.repeats * (
            sum({"A": 1, "B": 1, "C": 3, "D": 4}[a] for a in arms))
        print(f"Dataset: {len(dataset)} requests x {args.repeats} repeat(s), arms {arms}.")
        print(f"Approximate LLM-call budget: ~{budget} calls (C/D are estimates; depends on plan size).")
        if not args.yes:
            print("Dry run only. Re-run with --yes to call the model and write the CSV.")
        return

    print(f"Running arms {arms}, {args.repeats} repeat(s) over {len(dataset)} requests...")
    rows = run(arms, args.repeats)
    write_csv(rows, args.out)
    print(f"\nWrote {len(rows)} rows to {args.out}\n")
    print(format_summary(summarize(rows)))


if __name__ == "__main__":
    main()
