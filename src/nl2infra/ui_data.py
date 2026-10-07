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


def arm_consistency(label: str, logs: Dict[str, Optional[dict]]) -> Tuple[Optional[str], List[str]]:
    """Checks that the dropdown label and every arm's log are about one benchmark request.

    Returns (request id, problems). The id comes from the label ('<id> (repeat n)');
    each log must carry that id and repeat in its `benchmark` block, and all logs
    must hold the same prompt. Any problem means the panel must not be shown.
    """
    match = re.match(r"(.+) \(repeat (\d+)\)$", label or "")
    if not match:
        return None, [f"'{label}' is not a benchmark request label"]
    request_id, repeat = match.group(1), int(match.group(2))
    problems, prompts = [], set()
    for arm, log in sorted(logs.items()):
        if not log:
            continue
        bench = log.get("benchmark") or {}
        if bench.get("id") != request_id or bench.get("repeat") != repeat or bench.get("arm", arm) != arm:
            problems.append(f"arm {arm} log is for {bench.get('id')} repeat {bench.get('repeat')} arm {bench.get('arm')}, "
                            f"not {request_id} repeat {repeat} arm {arm}")
        prompts.add(log.get("prompt") or log.get("user_prompt"))
    if len(prompts) > 1:
        problems.append("the arm logs hold different request texts")
    return request_id, problems


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


def has_real_line(v: dict) -> bool:
    """True if the tool pointed at a specific line. Line 1 is only where the resource starts (Checkov reports
    that for every violation), so it says nothing about where the problem is."""
    return isinstance(v.get("line"), int) and v["line"] > 1


def gutter_marks(violations: List[dict], filename: str) -> Dict[int, List[str]]:
    """line number -> rule IDs, for violations that point at a real line of the file (greater than 1)."""
    marks: Dict[int, List[str]] = {}
    for v in violations:
        if v.get("file") == filename and has_real_line(v):
            rules = marks.setdefault(v["line"], [])
            if v["rule_id"] not in rules:
                rules.append(v["rule_id"])
    return marks


def default_file(names: List[str], violations: List[dict], changed: List[str]) -> str:
    """The file to open first: the first one with violations, else the first that changed, else the first."""
    failing = {v.get("file") for v in violations}
    for candidates in (failing, set(changed)):
        for name in names:
            if name in candidates:
                return name
    return names[0]


def file_failures(violations: List[dict], filename: str) -> List[Tuple[str, str]]:
    """(rule ID, message) for every violation on this file, once each, for the strip above the diff."""
    seen, failures = set(), []
    for v in violations:
        if v.get("file") == filename and v["rule_id"] not in seen:
            seen.add(v["rule_id"])
            failures.append((v["rule_id"], v.get("message") or ""))
    return failures


def where(v: dict) -> str:
    """'line N' when the tool gave a real line, otherwise the resource the violation is about."""
    return f"line {v['line']}" if has_real_line(v) else (v.get("resource") or "-")


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


def compact_rows(rows: List[dict], context: int = 3) -> List[dict]:
    """Only the changed hunks, with `context` unchanged lines either side.

    Runs of hidden lines become one row {"tag": "skip", "count": n}. The kept rows
    are the same objects, so their line numbers stay those of the full files.
    """
    changed = [i for i, row in enumerate(rows) if row["tag"] != "equal"]
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - context), min(len(rows), i + context + 1)))
    compact: List[dict] = []
    hidden = 0
    for i, row in enumerate(rows):
        if i in keep:
            if hidden:
                compact.append({"tag": "skip", "count": hidden})
                hidden = 0
            compact.append(row)
        else:
            hidden += 1
    if hidden:
        compact.append({"tag": "skip", "count": hidden})
    return compact


def diff_stats(rows: List[dict]) -> Tuple[int, int]:
    """(lines removed, lines added)."""
    removed = sum(1 for r in rows if r["tag"] in ("removed", "changed"))
    added = sum(1 for r in rows if r["tag"] in ("added", "changed"))
    return removed, added


# ------------------------------------------------------------------ plain wording

def plain_reason(text: Optional[str]) -> str:
    """A reason without Python exception names, e.g. 'RuntimeError: disk full' -> 'disk full'."""
    cleaned = re.sub(r"\b[A-Z][A-Za-z]*(?:Error|Exception|Timeout|Warning)\b:?\s*", "", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


_FIXER_INSTRUCTIONS = (
    r"\s*Remove it; do not add resources\.",
    r"\s*Use exactly the planned image string\..*$",
    r";\s*keep one definition\.",
)


def difference_only(message: Optional[str]) -> str:
    """A plan-conformance message without the instruction to the fixer: only what differs."""
    text = message or ""
    for pattern in _FIXER_INSTRUCTIONS:
        text = re.sub(pattern, "", text)
    text = text.strip()
    return text if text.endswith(".") or not text else text + "."


def pull_request_note(log: dict) -> Tuple[str, str]:
    """(headline, muted detail) for the pull-request line of a passed run."""
    if log.get("pr_url"):
        return f"Pull request: {log['pr_url']}", ""
    error = log.get("pr_error") or ""
    if "benchmark" in error.lower() or log.get("benchmark"):
        return "Pull request not opened: benchmark run", ""
    if error.startswith("file://") or "GITHUB_TOKEN" in error:
        return "Pull request not opened: no GitHub token", ""
    return "Pull request not opened", plain_reason(error)


# ------------------------------------------------------------------ benchmark arms

def arm_verdict(log: Optional[dict]) -> dict:
    """Pass/fail, counts and the violation lists for one arm, from the benchmark log written by run_benchmark.py."""
    if not log:
        return {"available": False}
    summary = log.get("summary") or {}
    final = summary.get("final") or {}
    rounds = log.get("rounds") or []
    last = rounds[-1]["violations"] if rounds else []
    malformed = ("YAML_SYNTAX_ERROR", "PLAN_CONFORMANCE_INVALID_DOC")
    return {
        "available": True,
        "outcome": summary.get("outcome"),
        "passed": final.get("safety_pass") is True,
        "violations": final.get("safety_violations"),
        "conformance_violations": final.get("conformance_violations"),
        "fix_rounds": summary.get("fix_rounds", log.get("iterations", 0)),
        "tokens": (summary.get("tokens_in") or 0) + (summary.get("tokens_out") or 0) if "tokens_in" in summary else None,
        "runtime": summary.get("runtime_seconds"),
        # what decides the verdict: Checkov, OPA, dry-run, and output that could not be parsed
        "violation_list": [v for v in last if v.get("tool") != "plan" or v.get("rule_id") in malformed],
        # differences from the shared reference plan; reported, but they do not decide the verdict
        "off_plan_list": [v for v in last if v.get("tool") == "plan" and v.get("rule_id") not in malformed],
        "files": rounds[-1]["files"] if rounds else {},
    }
