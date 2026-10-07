#!/usr/bin/env python3
"""Three-arm benchmark. Same model, temperature 0.1, same Gauntlet for every arm.

  A = one plain LLM call, no rules
  B = one LLM call with the rule list in the prompt
  C = the full pipeline (approval auto-confirmed); its generate prompt carries the
      same rule list as B, so the only difference between B and C is the fix loop

One planner call is made per request and repeat and shared by all three arms, so
plan conformance is measured against the same plan everywhere. Its tokens are
recorded separately and are not counted against any arm.

Correctness runs bypass the guardrails node: guardrails are measured on their own
(eval/evaluate.py), not by these arms. The bypass is recorded in every run log.

Nothing is sent to the model without --yes. Use --dry-count to see the call count.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "nl2infra"))
sys.path.insert(0, ROOT)

from contracts import Plan, RunState  # noqa: E402
from guardrails import check_guardrails  # noqa: E402
from llm_service import LLMBadOutput, LLMError, LLMUnavailable, _extract_yaml  # noqa: E402
from rules import OPA_RULES, RULE_LIST_FILE, rules_for_role  # noqa: E402,F401
from storage import git_commit, run_log  # noqa: E402
from validators import is_tool_crash  # noqa: E402

ARMS = ("A", "B", "C")
MAX_FIX_ROUNDS = 5
DEFAULT_FILE = os.path.join(ROOT, "benchmarks", "correctness.jsonl")

ARM_A_PROMPT = """Write the Kubernetes YAML manifests for this request.

Request: {prompt}

Reply with the YAML only, as one multi-document YAML (documents separated by ---). No explanation.
"""

ARM_B_PROMPT = """Write the Kubernetes YAML manifests for this request.

Request: {prompt}

The manifests will be checked against every rule below. All of them must pass.
{rules}

Reply with the YAML only, as one multi-document YAML (documents separated by ---). No explanation.
"""


class NoPublishGitOps:
    """Benchmark runs never open pull requests or write manifests anywhere."""
    def commit_and_pr(self, files, request_id, environment="dev"):
        raise RuntimeError("publishing is disabled for benchmark runs")


# ------------------------------------------------------------------ rule list

def build_rule_list(checkov_bin: str, skipped: List[str]) -> str:
    """The Checkov rules that apply to ordinary resources, taken from Checkov itself, minus the skipped ones."""
    proc = subprocess.run([checkov_bin, "--list", "--framework", "kubernetes"], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"checkov --list failed: {proc.stderr[:200]}")
    names: Dict[str, str] = {}
    entities: Dict[str, set] = {}
    for line in proc.stdout.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) > 5 and cells[2].startswith("CKV"):
            names.setdefault(cells[2], cells[5])
            entities.setdefault(cells[2], set()).add(cells[4])
    ordinary = {
        "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "Service", "ConfigMap", "Secret",
        "Ingress", "ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding",
        "PersistentVolumeClaim", "NetworkPolicy", "containers", "initContainers",
    }
    control_plane = re.compile(r"--|kubelet|apiserver|api server|etcd|controller-manager|scheduler|encryption providers|admission control plugin", re.I)
    lines = [
        f"{rule}: {name}" for rule, name in names.items()
        if entities[rule] & ordinary and not control_plane.search(name) and rule not in skipped
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ scoring

def score(violations: List[dict], scan: dict) -> dict:
    """Splits one Gauntlet result into the safety verdict and the plan-conformance verdict.

    safety_pass: Checkov, OPA and server dry-run all ran on every file and found nothing.
    Unparseable output, or output with no resources, is not a pass.
    conformance_pass: the files match the shared plan exactly.
    """
    crash = any(v["rule_id"] == "TOOL_CRASH" for v in violations)
    malformed = [v for v in violations if v["rule_id"] in ("YAML_SYNTAX_ERROR", "PLAN_CONFORMANCE_INVALID_DOC")]
    safety = [v for v in violations if v["tool"] in ("checkov", "opa", "dry-run")]
    conformance = [v for v in violations if v["tool"] == "plan" and v not in malformed]
    fully_scanned = scan.get("resources", 0) > 0 and scan.get("scanned") == scan.get("files")
    safety_pass = not crash and not safety and not malformed and fully_scanned
    return {
        "tool_crash": crash,
        "safety_violations": len(safety) + len(malformed),
        "conformance_violations": len(conformance),
        "safety_pass": safety_pass,
        "conformance_pass": not crash and not malformed and not conformance and fully_scanned,
        "all_four_pass": safety_pass and not conformance,
    }


def summarise(log: dict) -> dict:
    """The numbers evaluate.py reads, computed once from the per-round record."""
    rounds = log.get("rounds") or []
    calls = [c for c in log.get("llm_calls", []) if c.get("stage") != "plan"]
    waited = sum(c.get("waited_seconds") or 0 for c in calls)
    metrics = log.get("metrics", {})
    if log["arm"] == "C":
        runtime = sum(metrics.get(k, 0.0) for k in ("generation_time", "validation_time", "fix_time")) - waited
    else:
        runtime = sum(c.get("seconds") or 0 for c in calls)
    unknown_tokens = sum(1 for c in calls if c.get("ok") and c.get("tokens_in") is None)
    summary = {
        "outcome": "scored" if rounds else "no_output",
        "first": score(rounds[0]["violations"], rounds[0].get("scan", {})) if rounds else None,
        "final": score(rounds[-1]["violations"], rounds[-1].get("scan", {})) if rounds else None,
        "fix_rounds": log.get("iterations", 0),
        "runtime_seconds": round(runtime, 3),
        "tokens_in": sum(c.get("tokens_in") or 0 for c in calls),
        "tokens_out": sum(c.get("tokens_out") or 0 for c in calls),
        "llm_calls": len(calls),
        "calls_without_token_counts": unknown_tokens,
    }
    reason = log.get("rejection_reason") or ""
    if reason.startswith("llm_unavailable"):
        summary["outcome"] = "llm_unavailable"       # not a result; --resume runs it again
    elif reason.startswith("tool_crash") or (summary["final"] and summary["final"]["tool_crash"]):
        summary["outcome"] = "tool_crash"            # not a result; fix the environment and --resume
    elif reason.startswith("internal_error"):
        summary["outcome"] = "internal_error"
    return summary


# ------------------------------------------------------------------ arms

def new_state(request: dict, arm: str, repeat: int, run_id: str, plan: Plan, llm, validator) -> RunState:
    return RunState(
        request_id=run_id, user_role=request["role"], user_prompt=request["prompt"],
        model=llm.model_name, temperature=llm.temperature, plan=plan, approved_plan=plan, plan_source="shared",
        tool_versions=validator.tool_versions(), skipped_checks=validator.skipped_checks(), commit=git_commit(),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), arm=arm, guardrails_bypassed=True,
        benchmark={"id": request["id"], "tier": request.get("tier"), "arm": arm, "repeat": repeat,
                   "expected": request.get("expected")},
    )


def run_single_call_arm(arm: str, request: dict, repeat: int, run_id: str, plan: Plan, llm, validator) -> RunState:
    """Arms A and B: one LLM call, then the same Gauntlet as arm C. No fix loop."""
    state = new_state(request, arm, repeat, run_id, plan, llm, validator)
    if arm == "A":
        prompt = ARM_A_PROMPT.format(prompt=request["prompt"])
    else:
        prompt = ARM_B_PROMPT.format(prompt=request["prompt"], rules=rules_for_role(request["role"]))
    try:
        state.files = {"manifests.yaml": _extract_yaml(llm._call(prompt, f"arm_{arm.lower()}"))}
    except LLMBadOutput as error:
        state.files = {"manifests.yaml": ""}
        state.rejection_reason = f"{error.reason}: {error}"
    except LLMError as error:
        state.llm_calls = llm.drain_calls()
        state.status = "escalated"
        state.rejection_reason = f"{error.reason}: {error}"
        return state
    state.llm_calls = llm.drain_calls()

    started = time.time()
    state.violations = validator.validate(state.files, plan, role=request["role"])
    state.metrics["validation_time"] = round(time.time() - started, 3)
    state.rounds = [{
        "round": 0, "files": dict(state.files), "violations": [v.model_dump() for v in state.violations],
        "tool_seconds": dict(validator.last_timings), "scan": dict(validator.last_scan),
    }]
    if is_tool_crash(state.violations):
        state.status = "escalated"
        state.rejection_reason = "tool_crash: " + "; ".join(
            f"{v.tool}: {v.message}" for v in state.violations if v.rule_id == "TOOL_CRASH")
    else:
        state.status = "passed" if not state.violations else "escalated"
    return state


def run_pipeline_arm(request: dict, repeat: int, run_id: str, plan: Plan, pipeline) -> RunState:
    """Arm C: the real graph, given the shared plan, approval auto-confirmed, guardrails bypassed."""
    return pipeline.start(
        request["prompt"], request["role"], request_id=run_id, arm="C", plan=plan, bypass_guardrails=True,
        benchmark={"id": request["id"], "tier": request.get("tier"), "arm": "C", "repeat": repeat,
                   "expected": request.get("expected")},
    )


# ------------------------------------------------------------------ files

def load_requests(path: str, tier: Optional[str], limit: Optional[int]) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        requests = [json.loads(line) for line in f if line.strip()]
    if tier:
        requests = [r for r in requests if r.get("tier") == tier]
    return requests[:limit] if limit else requests


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # a killed run never leaves a half-written log


def is_done(path: str) -> bool:
    """A finished run is one with a result. LLM outages and tool crashes are run again on --resume."""
    log = read_json(path)
    return bool(log) and log.get("summary", {}).get("outcome") in ("scored", "no_output", "plan_failed")


# ------------------------------------------------------------------ counting

def count_calls(requests: List[dict], arms: List[str], repeats: int) -> dict:
    resources = [int(r.get("resources") or 0) for r in requests]
    n = len(requests) * repeats
    per_arm = {
        "plan (shared)": (n, 2 * n),                       # one call; a rejected plan is retried once
        "A": (n, n) if "A" in arms else (0, 0),
        "B": (n, n) if "B" in arms else (0, 0),
        # generate: one call per resource. fix: 0 to 5 rounds, at most one call per file per round.
        "C": (sum(resources) * repeats, sum(resources) * (1 + MAX_FIX_ROUNDS) * repeats) if "C" in arms else (0, 0),
    }
    return {"per_arm": per_arm, "min": sum(lo for lo, _ in per_arm.values()), "max": sum(hi for _, hi in per_arm.values()),
            "runs": n * len(arms), "unknown_sizes": [r["id"] for r in requests if not r.get("resources")]}


def print_dry_count(requests: List[dict], arms: List[str], repeats: int, out_dir: str, resume: bool) -> None:
    counts = count_calls(requests, arms, repeats)
    print(f"Requests: {len(requests)}   repeats: {repeats}   arms: {', '.join(arms)}   runs: {counts['runs']}")
    print(f"{'':16}{'min calls':>10}{'max calls':>11}")
    for name, (lo, hi) in counts["per_arm"].items():
        print(f"{name:16}{lo:>10}{hi:>11}")
    print(f"{'TOTAL':16}{counts['min']:>10}{counts['max']:>11}")
    print("min = every plan accepted first time and no fix needed. max = every plan retried once and every")
    print("file re-sent in all 5 fix rounds. Arm C sizes use the 'resources' field of each request; the real")
    print("plan may differ. Retries after a timeout, 5xx or HTTP 429 are extra and not counted here.")
    if counts["unknown_sizes"]:
        print(f"No 'resources' field, so arm C is undercounted for: {', '.join(counts['unknown_sizes'])}")
    if resume:
        done = sum(is_done(os.path.join(out_dir, f"{r['id']}-{a}-r{k}.json"))
                   for r in requests for a in arms for k in range(1, repeats + 1))
        print(f"--resume: {done} of {counts['runs']} runs already have a result in {out_dir} and will be skipped.")

    refused = [(r["id"], reason) for r in requests if (reason := check_guardrails(
        RunState(request_id="dry", user_role=r["role"], user_prompt=r["prompt"], model="none")))]
    print(f"\nGuardrails are bypassed for these runs (they are measured separately by eval/evaluate.py).")
    print(f"For information, the guardrails node would refuse {len(refused)} of {len(requests)} of these requests"
          + (":" if refused else "."))
    for request_id, reason in refused:
        print(f"  {request_id}: {reason}")


# ------------------------------------------------------------------ main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", default="all", help="A, B, C or all (default)")
    parser.add_argument("--tier", choices=["easy", "medium", "hard"], help="only requests of this tier")
    parser.add_argument("--limit", type=int, help="only the first N requests after the tier filter")
    parser.add_argument("--repeats", type=int, default=3, help="repeats per request (default 3)")
    parser.add_argument("--resume", action="store_true", help="skip runs that already have a result")
    parser.add_argument("--dry-count", action="store_true", help="print the number of LLM calls and exit")
    parser.add_argument("--yes", action="store_true", help="really call the model; without it nothing is sent")
    parser.add_argument("--file", default=DEFAULT_FILE, help="benchmark file (default benchmarks/correctness.jsonl)")
    parser.add_argument("--out", default=os.path.join(ROOT, "runs", "benchmark"), help="output folder for run logs")
    parser.add_argument("--write-rule-list", action="store_true", help="regenerate benchmarks/rule_list.txt from Checkov and exit")
    args = parser.parse_args(argv)

    arms = list(ARMS) if args.arm.lower() == "all" else [args.arm.upper()]
    if any(a not in ARMS for a in arms):
        parser.error("--arm must be A, B, C or all")

    if args.write_rule_list:
        from validators import Validators
        validator = Validators()
        header = (f"# Checkov {validator.tool_versions()['checkov']} Kubernetes rules for ordinary resources, from "
                  f"`checkov --list`.\n# Skipped rules ({', '.join(validator.skipped_checks())}) and control-plane "
                  "rules are left out. Regenerate: run_benchmark.py --write-rule-list\n")
        with open(RULE_LIST_FILE, "w", encoding="utf-8") as f:
            f.write(header + build_rule_list(validator.checkov_bin, validator.skipped_checks()))
        print(f"wrote {RULE_LIST_FILE}")
        return 0

    requests = load_requests(args.file, args.tier, args.limit)
    if not requests:
        print("No requests match.")
        return 1

    print_dry_count(requests, arms, args.repeats, args.out, args.resume)
    if args.dry_count:
        return 0
    if not args.yes:
        print("\nNothing was sent to the model. Add --yes to run.")
        return 0

    os.environ["MOCK_MODE"] = "false"
    from llm_service import LLMService
    from pipeline import Pipeline
    from validators import Validators
    llm, validator = LLMService(), Validators()
    pipeline = Pipeline(llm, validator, NoPublishGitOps(), auto_approve=True, run_dir=os.path.join(args.out, "_pipeline"))
    print(f"\nModel: {llm.model_name}   temperature: {llm.temperature}   tools: {validator.tool_versions()}")

    for request in requests:
        for repeat in range(1, args.repeats + 1):
            paths = {arm: os.path.join(args.out, f"{request['id']}-{arm}-r{repeat}.json") for arm in arms}
            todo = [arm for arm in arms if not (args.resume and is_done(paths[arm]))]
            if not todo:
                continue

            # The shared plan: made once, reused by every arm and by --resume.
            plan_path = os.path.join(args.out, "plans", f"{request['id']}-r{repeat}.json")
            plan_record = read_json(plan_path)
            if not plan_record:
                try:
                    plan = llm.plan(request["prompt"], request["role"])
                    plan_record = {"plan": plan.model_dump(), "error": None}
                except LLMUnavailable as error:
                    print(f"STOPPED: the model is unavailable ({error}). Run again with --resume.")
                    return 2
                except LLMBadOutput as error:
                    plan_record = {"plan": None, "error": str(error)}
                plan_record.update(id=request["id"], repeat=repeat, model=llm.model_name, llm_calls=llm.drain_calls())
                write_json(plan_path, plan_record)

            for arm in todo:
                run_id = f"{request['id']}-{arm}-r{repeat}"
                if plan_record["plan"] is None:
                    log = {"request_id": run_id, "arm": arm, "benchmark": {"id": request["id"], "tier": request.get("tier"),
                           "arm": arm, "repeat": repeat}, "rejection_reason": f"plan_failed: {plan_record['error']}",
                           "summary": {"outcome": "plan_failed"}}
                else:
                    plan = Plan.model_validate(plan_record["plan"])
                    if arm == "C":
                        state = run_pipeline_arm(request, repeat, run_id, plan, pipeline)
                    else:
                        state = run_single_call_arm(arm, request, repeat, run_id, plan, llm, validator)
                    log = run_log(state)
                    log["plan_llm_calls"] = plan_record["llm_calls"]
                    log["summary"] = summarise(log)
                write_json(paths[arm], log)

                summary = log["summary"]
                final = summary.get("final") or {}
                print(f"{run_id:22} {summary['outcome']:15} safety_pass={final.get('safety_pass')} "
                      f"violations={final.get('safety_violations')} conformance_pass={final.get('conformance_pass')} "
                      f"fix_rounds={summary.get('fix_rounds')} tokens={summary.get('tokens_in')}/{summary.get('tokens_out')}")
                if summary["outcome"] == "llm_unavailable":
                    print("STOPPED: the model is unavailable. Run again with --resume.")
                    return 2
                if summary["outcome"] == "tool_crash":
                    print(f"STOPPED: a checker failed to run ({log.get('rejection_reason')}). Fix it, then --resume.")
                    return 3
    print(f"\nDone. Run logs: {args.out}   Results: python eval/evaluate.py --runs {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
