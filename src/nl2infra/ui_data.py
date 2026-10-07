"""What the UI shows, worked out from a run log. No Streamlit here, so it can be tested.

Everything is derived from the run-log dictionary (storage.run_log): the UI never
calls the model, the scanners or the cluster to draw a finished run.
"""
import difflib
import json
import os
import re
from typing import Dict, List, Optional, Tuple

TOOLS = ("checkov", "opa", "dry-run", "plan")
TOOL_LABELS = {"checkov": "Checkov", "opa": "OPA", "dry-run": "Dry-run", "plan": "Plan"}
STATUS_ORDER = {"still failing": 0, "new in this round": 1, "fixed in this round": 2}


# ------------------------------------------------------------------ files

def list_run_logs(runs_dir: str) -> List[str]:
    """Run logs under runs_dir, newest first, as paths relative to it. Plans and the pipeline's duplicates are left out."""
    found = []
    for root, dirs, names in os.walk(runs_dir):
        dirs[:] = [d for d in dirs if d not in ("plans", "_pipeline")]
        for name in names:
            if name.endswith(".json"):
                path = os.path.join(root, name)
                found.append((os.path.getmtime(path), os.path.relpath(path, runs_dir)))
    return [path for _, path in sorted(found, reverse=True)]


def load_log(path: str) -> Optional[dict]:
    """A run log, or None if the file is not one."""
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return log if isinstance(log, dict) and "request_id" in log and "status" in log else None


def benchmark_folders(runs_dir: str) -> List[str]:
    """Folders under runs_dir that hold benchmark logs (named <id>-<arm>-r<n>.json)."""
    folders = []
    for root, dirs, names in os.walk(runs_dir):
        dirs[:] = [d for d in dirs if d not in ("plans", "_pipeline")]
        if any(re.match(r".+-[ABC]-r\d+\.json$", name) for name in names):
            folders.append(os.path.relpath(root, runs_dir))
    return sorted(folders)


def benchmark_requests(folder: str) -> Dict[str, Dict[str, str]]:
    """'<id> (repeat n)' -> {arm: path} for one benchmark folder."""
    requests: Dict[str, Dict[str, str]] = {}
    for name in sorted(os.listdir(folder)):
        match = re.match(r"(.+)-([ABC])-r(\d+)\.json$", name)
        if match:
            requests.setdefault(f"{match.group(1)} (repeat {match.group(3)})", {})[match.group(2)] = os.path.join(folder, name)
    return requests


# ------------------------------------------------------------------ rounds

def tool_counts(violations: List[dict]) -> Dict[str, int]:
    counts = {tool: 0 for tool in TOOLS}
    for v in violations:
        counts[v.get("tool")] = counts.get(v.get("tool"), 0) + 1
    return counts


def round_label(number: int) -> str:
    return "Draft 1" if number == 0 else f"Fix {number}"


def stopped_at(log: dict) -> str:
    """The stage a run ended in when it never produced a draft."""
    reason = (log.get("rejection_reason") or "").split(":")[0]
    if reason == "plan_not_approved":
        return "Approval"
    if log.get("status") == "rejected":
        return "Guardrails"
    if not log.get("plan"):
        return "Plan"
    return "Generate"


def timeline(log: dict, awaiting_approval: bool = False) -> List[dict]:
    """The loop as steps: one per Gauntlet round, then how the run ended.

    Round steps: {"kind": "round", "round", "label", "count", "tools"}.
    The last step: {"kind": "end", "label", "passed", "reason"}; its label is the
    run log's status and nothing else, so PASSED appears only for a passed run.
    """
    rounds = log.get("rounds") or []
    steps: List[dict] = [{
        "kind": "round", "round": entry["round"], "label": round_label(entry["round"]),
        "count": len(entry["violations"]), "tools": tool_counts(entry["violations"]),
    } for entry in rounds]

    status = log.get("status") or "running"
    reason = log.get("rejection_reason") or ""
    if not rounds and status != "running":
        steps.append({"kind": "stage", "label": stopped_at(log), "failed": True})
    elif rounds and status == "escalated" and (log.get("iterations") or 0) > rounds[-1]["round"]:
        # A fix round was started but produced no draft (model unavailable or unusable reply).
        steps.append({"kind": "stage", "label": round_label(log["iterations"]), "failed": True})

    if status == "running":
        label = "Awaiting approval" if awaiting_approval else "Running"
    else:
        label = status.capitalize()
    steps.append({"kind": "end", "label": label, "passed": status == "passed", "running": status == "running",
                  "reason": reason})
    return steps


def violation_key(v: dict) -> Tuple:
    return (v.get("tool"), v.get("rule_id"), v.get("file"), v.get("resource"))


def violation_rows(rounds: List[dict], number: int) -> List[dict]:
    """Violations for one round, each with what happened to it in that round.

    'fixed in this round': failed before this round's fix and is gone after it.
    'still failing': failed before and after. 'new in this round': appeared after it.
    For the first draft everything is 'new in this round'.
    """
    current = rounds[number]["violations"]
    previous = rounds[number - 1]["violations"] if number > 0 else []
    current_keys = {violation_key(v) for v in current}
    previous_keys = {violation_key(v) for v in previous}
    rows = [dict(v, status="still failing" if violation_key(v) in previous_keys else "new in this round") for v in current]
    rows += [dict(v, status="fixed in this round") for v in previous if violation_key(v) not in current_keys]
    return sorted(rows, key=lambda r: (STATUS_ORDER[r["status"]], r.get("file") or "", r.get("rule_id") or ""))


def changed_files(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    return [name for name in after if before.get(name) != after[name]] + [name for name in before if name not in after]


def gutter_marks(violations: List[dict], filename: str) -> Dict[int, List[str]]:
    """line number -> rule IDs of the violations that point at that line of the file."""
    marks: Dict[int, List[str]] = {}
    for v in violations:
        if v.get("file") == filename and v.get("line"):
            rules = marks.setdefault(int(v["line"]), [])
            if v["rule_id"] not in rules:
                rules.append(v["rule_id"])
    return marks


def unplaced_rules(violations: List[dict], filename: str) -> List[str]:
    """Rule IDs for this file that carry no line number, so they cannot be shown in the gutter."""
    return sorted({v["rule_id"] for v in violations if v.get("file") == filename and not v.get("line")})


# ------------------------------------------------------------------ diff

def diff_rows(before: str, after: str) -> List[dict]:
    """Aligned rows for a side-by-side diff.

    Each row: left_no, left, right_no, right, tag. tag is 'equal', 'removed'
    (left only), 'added' (right only) or 'changed' (both sides, different).
    """
    left, right = before.splitlines(), after.splitlines()
    rows: List[dict] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if op == "equal":
            for offset in range(i2 - i1):
                rows.append({"left_no": i1 + offset + 1, "left": left[i1 + offset],
                             "right_no": j1 + offset + 1, "right": right[j1 + offset], "tag": "equal"})
            continue
        for offset in range(max(i2 - i1, j2 - j1)):
            has_left, has_right = i1 + offset < i2, j1 + offset < j2
            rows.append({
                "left_no": i1 + offset + 1 if has_left else None, "left": left[i1 + offset] if has_left else "",
                "right_no": j1 + offset + 1 if has_right else None, "right": right[j1 + offset] if has_right else "",
                "tag": "changed" if has_left and has_right else "removed" if has_left else "added",
            })
    return rows


def diff_stats(rows: List[dict]) -> Tuple[int, int]:
    """(lines removed, lines added)."""
    removed = sum(1 for r in rows if r["tag"] in ("removed", "changed"))
    added = sum(1 for r in rows if r["tag"] in ("added", "changed"))
    return removed, added


# ------------------------------------------------------------------ benchmark arms

def arm_verdict(log: Optional[dict]) -> dict:
    """Pass/fail and counts for one arm, from the benchmark summary written by run_benchmark.py."""
    if not log:
        return {"available": False}
    final = (log.get("summary") or {}).get("final") or {}
    rounds = log.get("rounds") or []
    return {
        "available": True,
        "outcome": (log.get("summary") or {}).get("outcome"),
        "passed": final.get("safety_pass") is True,
        "violations": final.get("safety_violations"),
        "conformance_violations": final.get("conformance_violations"),
        "fix_rounds": (log.get("summary") or {}).get("fix_rounds", log.get("iterations", 0)),
        "files": rounds[-1]["files"] if rounds else {},
    }
