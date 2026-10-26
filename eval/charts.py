#!/usr/bin/env python3
"""Render benchmark charts from the run logs.

  python eval/charts.py --runs runs/benchmark --out results/charts

Produces one PNG per metric comparing the arms (A/B/C/D): final pass rate, draft-1
violations, fix-loop rounds, tokens and runtime. Reads the same logs as evaluate.py.
Uses the Agg backend so it works headless.
"""
import argparse
import json
import os
import statistics
import sys
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ARMS = ("A", "B", "C", "D")
ARM_NAMES = {"A": "A plain", "B": "B rules", "C": "C pipeline", "D": "D +RAG"}
SCORED = ("scored", "no_output")


def load_logs(runs_dir: str) -> List[dict]:
    logs = []
    if not os.path.isdir(runs_dir):
        return logs
    for name in sorted(os.listdir(runs_dir)):
        if name.endswith(".json"):
            with open(os.path.join(runs_dir, name), encoding="utf-8") as f:
                log = json.load(f)
            if "summary" in log and log.get("arm") in ARMS:
                logs.append(log)
    return logs


def _aggregate(logs: List[dict]) -> Dict[str, dict]:
    """Per-arm raw aggregates for charting."""
    out = {}
    for arm in ARMS:
        scored = [l["summary"] for l in logs if l["arm"] == arm and l["summary"]["outcome"] in SCORED]
        drafts = [s for s in scored if s.get("first")]
        if not scored:
            continue
        out[arm] = {
            "final_pass_rate": 100 * sum(s["final"]["safety_pass"] for s in drafts) / len(scored) if scored else 0,
            "draft1_violations": statistics.mean([s["first"]["safety_violations"] for s in drafts]) if drafts else 0,
            "fix_rounds": statistics.mean([s["fix_rounds"] for s in scored]),
            "tokens": statistics.median([s["tokens_in"] + s["tokens_out"] for s in scored]),
            "runtime": statistics.median([s["runtime_seconds"] for s in scored]),
        }
    return out


def _bar(values: Dict[str, dict], key: str, title: str, ylabel: str, path: str) -> None:
    arms = [a for a in ARMS if a in values]
    heights = [values[a][key] for a in arms]
    fig, ax = plt.subplots(figsize=(5, 3.2))
    bars = ax.bar([ARM_NAMES[a] for a in arms], heights, color=["#6d6a60", "#4a7fd6", "#15150f", "#28a745"][: len(arms)])
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    for bar, h in zip(bars, heights):
        ax.text(bar.get_x() + bar.get_width() / 2, h, f"{h:.1f}", ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def make_charts(runs_dir: str, out_dir: str) -> List[str]:
    logs = load_logs(runs_dir)
    values = _aggregate(logs)
    if not values:
        return []
    os.makedirs(out_dir, exist_ok=True)
    charts = [
        ("final_pass_rate", "Final pass rate by arm", "pass rate (%)", "pass_rate.png"),
        ("draft1_violations", "Violations per request (draft 1)", "mean violations", "violations.png"),
        ("fix_rounds", "Mean fix-loop rounds by arm", "rounds", "fix_rounds.png"),
        ("tokens", "Median tokens (in + out) by arm", "tokens", "tokens.png"),
        ("runtime", "Median runtime by arm", "seconds", "runtime.png"),
    ]
    written = []
    for key, title, ylabel, filename in charts:
        path = os.path.join(out_dir, filename)
        _bar(values, key, title, ylabel, path)
        written.append(path)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default=os.path.join(os.path.dirname(__file__), "..", "runs", "benchmark"))
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results", "charts"))
    args = parser.parse_args(argv)
    written = make_charts(args.runs, args.out)
    if not written:
        print(f"No scored benchmark logs in {args.runs}; nothing to chart.")
        return 1
    print("Wrote:\n" + "\n".join("  " + p for p in written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
