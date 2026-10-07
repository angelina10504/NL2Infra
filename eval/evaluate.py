#!/usr/bin/env python3
"""Prints the benchmark results table from the run logs, and the guardrail rates.

  python eval/evaluate.py --runs runs/benchmark

Every percentage is printed with its counts. A run counts as a pass when Checkov,
OPA and the server dry-run report nothing on its final files. Plan conformance is
a separate row and does not decide a pass. Runs that have no result (model
unavailable, checker crashed, plan rejected) are listed, not scored.
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter
from typing import List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "nl2infra"))
sys.path.insert(0, ROOT)

ARMS = ("A", "B", "C")
ARM_NAMES = {"A": "A plain", "B": "B rules in prompt", "C": "C full pipeline"}
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


def rate(hits: int, total: int) -> str:
    return f"{100 * hits / total:.0f}% ({hits}/{total})" if total else "n/a (0/0)"


def mean(values: list) -> str:
    return f"{statistics.mean(values):.2f} (n={len(values)})" if values else "n/a (n=0)"


def median(values: list, unit: str = "") -> str:
    return f"{statistics.median(values):.1f}{unit} (n={len(values)})" if values else "n/a (n=0)"


def arm_rows(logs: List[dict]) -> dict:
    """One column of the results table."""
    scored = [log["summary"] for log in logs if log["summary"]["outcome"] in SCORED]
    with_draft = [s for s in scored if s["first"]]
    n = len(scored)
    return {
        "Runs scored": str(n),
        "First-draft pass rate": rate(sum(s["first"]["safety_pass"] for s in with_draft), n),
        "Violations per request, draft 1": mean([s["first"]["safety_violations"] for s in with_draft]),
        "Final pass rate": rate(sum(s["final"]["safety_pass"] for s in with_draft), n),
        "Mean fix rounds": mean([s["fix_rounds"] for s in scored]),
        "Plan conformance (final files)": rate(sum(s["final"]["conformance_pass"] for s in with_draft), n),
        "All four checks pass": rate(sum(s["final"]["all_four_pass"] for s in with_draft), n),
        "Median runtime": median([s["runtime_seconds"] for s in scored], " s"),
        "Median tokens (in + out)": median([s["tokens_in"] + s["tokens_out"] for s in scored]),
        "Median tokens in": median([s["tokens_in"] for s in scored]),
        "Median tokens out": median([s["tokens_out"] for s in scored]),
    }


def print_table(columns: dict) -> None:
    labels = list(next(iter(columns.values())))
    width = max(len(label) for label in labels) + 2
    col = max(22, *(len(v) + 2 for rows in columns.values() for v in rows.values()))
    print(f"{'':{width}}" + "".join(f"{ARM_NAMES[arm]:<{col}}" for arm in columns))
    for label in labels:
        print(f"{label:{width}}" + "".join(f"{columns[arm][label]:<{col}}" for arm in columns))


def print_benchmark(runs_dir: str, tier: Optional[str]) -> None:
    logs = load_logs(runs_dir)
    if tier:
        logs = [log for log in logs if log.get("benchmark", {}).get("tier") == tier]
    print(f"== Benchmark results: {runs_dir}" + (f" (tier: {tier})" if tier else "") + " ==")
    if not logs:
        print("No benchmark run logs found.\n")
        return

    requests = sorted({log["benchmark"]["id"] for log in logs})
    repeats = sorted({log["benchmark"]["repeat"] for log in logs})
    models = sorted({log.get("model_id") for log in logs if log.get("model_id")})
    tiers = Counter(log["benchmark"].get("tier") for log in logs if log["arm"] == logs[0]["arm"] and log["benchmark"]["repeat"] == repeats[0])
    print(f"Requests: {len(requests)} ({', '.join(f'{k}: {v}' for k, v in tiers.items())})   repeats: {len(repeats)}   "
          f"model: {', '.join(models) or 'unknown'}   temperature: {logs[0].get('temperature')}")
    print("Pass = 0 violations from Checkov, OPA and server dry-run on the final files. Plan conformance is")
    print("reported separately. Guardrails were bypassed for these runs. Arms B and C get the same rule list in the")
    print("prompt. Runtime and tokens exclude the shared planning call and any rate-limit waits.\n")

    arms = [arm for arm in ARMS if any(log["arm"] == arm for log in logs)]
    print_table({arm: arm_rows([log for log in logs if log["arm"] == arm]) for arm in arms})

    unscored = Counter((log["arm"], log["summary"]["outcome"]) for log in logs if log["summary"]["outcome"] not in SCORED)
    print("\nRuns without a result (not scored): " + (", ".join(
        f"arm {arm} {outcome}: {count}" for (arm, outcome), count in sorted(unscored.items())) or "none"))
    no_output = Counter(log["arm"] for log in logs if log["summary"]["outcome"] == "no_output")
    if no_output:
        print("Runs scored as a fail because the model produced nothing usable: "
              + ", ".join(f"arm {arm}: {count}" for arm, count in sorted(no_output.items())))
    missing_tokens = sum(log["summary"].get("calls_without_token_counts", 0) for log in logs)
    if missing_tokens:
        print(f"WARNING: {missing_tokens} call(s) returned no token counts; token medians are too low.")

    # Arm C: how the fix loop behaves, round by round.
    c_logs = [log for log in logs if log["arm"] == "C" and log["summary"]["outcome"] in SCORED]
    if c_logs:
        print("\nArm C, violations per round (all four checks; round 0 is the first draft):")
        for number in range(max(len(log.get("rounds") or []) for log in c_logs)):
            counts = [len(log["rounds"][number]["violations"]) for log in c_logs if len(log.get("rounds") or []) > number]
            print(f"  round {number}: mean {mean(counts)}")
        fixed = Counter(v["rule_id"] for log in c_logs for entry in log.get("rounds") or [] for v in entry["violations"])
        runs_with = Counter(rule for log in c_logs for rule in {
            v["rule_id"] for entry in log.get("rounds") or [] for v in entry["violations"]})
        print("Arm C, rule IDs that needed fixing most often (occurrences over all rounds; runs affected):")
        for rule, count in fixed.most_common(10):
            print(f"  {rule}: {count} ({runs_with[rule]}/{len(c_logs)} runs)")
        if not fixed:
            print("  none")

    # The shared planner, counted once per request and repeat.
    plans = {}
    for log in logs:
        plans.setdefault((log["benchmark"]["id"], log["benchmark"]["repeat"]), log.get("plan_llm_calls") or [])
    plan_tokens = [sum((c.get("tokens_in") or 0) + (c.get("tokens_out") or 0) for c in calls) for calls in plans.values() if calls]
    plan_calls = sum(1 for calls in plans.values() for c in calls if c.get("ok"))
    print(f"Shared planner (not counted in any arm): {plan_calls} call(s) for {len(plans)} plan(s), "
          f"median tokens per plan {median(plan_tokens)}")
    attempts = [c for log in logs for c in log.get("llm_calls", [])] + [c for calls in plans.values() for c in calls]
    limited = [c for c in attempts if c.get("http_status") == 429]
    print(f"LLM calls answered: {sum(1 for c in attempts if c.get('ok'))}. Attempts refused with HTTP 429 and "
          f"retried: {len(limited)} (waited {sum(c.get('waited_seconds') or 0 for c in limited):.0f} s in total).")

    # Which rules fail on the first draft.
    for arm in arms:
        rules = Counter(
            v["rule_id"] for log in logs if log["arm"] == arm and log["summary"]["outcome"] in SCORED
            for v in (log.get("rounds") or [{}])[0].get("violations", []) if v["tool"] != "plan"
        )
        top = ", ".join(f"{rule} x{count}" for rule, count in rules.most_common(8)) or "none"
        print(f"Draft-1 violations, arm {arm}: {top}")
    print()


def load_requests(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def print_guardrails(benchmarks_dir: str) -> None:
    """Guardrails are rules, so they are measured directly: no model call is involved."""
    from contracts import RunState
    from guardrails import check_guardrails

    def refused(requests):
        return [r["id"] for r in requests if check_guardrails(
            RunState(request_id=r["id"], user_role=r["role"], user_prompt=r["prompt"], model="none"))]

    adversarial = load_requests(os.path.join(benchmarks_dir, "adversarial.jsonl"))
    legitimate = load_requests(os.path.join(benchmarks_dir, "correctness.jsonl"))
    refused_adv, refused_legit = refused(adversarial), refused(legitimate)
    print("== Guardrails (deterministic, no LLM call) ==")
    print(f"Refusal rate on adversarial requests:       {rate(len(refused_adv), len(adversarial))}")
    print(f"False refusal rate on legitimate requests:  {rate(len(refused_legit), len(legitimate))}")
    if len(adversarial) != 40 or len(legitimate) != 30:
        print(f"NOTE: the benchmark files hold {len(adversarial)} adversarial and {len(legitimate)} legitimate requests; "
              "the specification calls for 40 and 30.")
    allowed = [r["id"] for r in adversarial if r["id"] not in refused_adv]
    if allowed:
        print(f"Adversarial requests let through: {', '.join(allowed)}")
    if refused_legit:
        print(f"Legitimate requests refused: {', '.join(refused_legit)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default=os.path.join(ROOT, "runs", "benchmark"), help="folder of benchmark run logs")
    parser.add_argument("--tier", choices=["easy", "medium", "hard"], help="only this tier")
    parser.add_argument("--benchmarks", default=os.path.join(ROOT, "benchmarks"), help="folder with the .jsonl files")
    args = parser.parse_args(argv)
    print_benchmark(args.runs, args.tier)
    print_guardrails(args.benchmarks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
