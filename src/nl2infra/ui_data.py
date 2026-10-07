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


# ------------------------------------------------------------------ explanatory layer

TOOL_GLOSS = {
    "checkov": "security scan",
    "opa": "our team rules",
    "dry-run": "would Kubernetes accept it",
    "plan": "matches what was approved",
}

# Plain-English name and one line on why it matters, for every rule id seen in runs/pilot/.
# `fields` are the YAML keys the rule is about; they are used only to pick which changed
# lines of the diff to show beside the rule. A rule id that is not listed here is shown
# with the checker's own message and no explanation: nothing is made up for it.
RULES = {
    "CKV_K8S_8": {"name": "No liveness probe", "why": "Kubernetes cannot tell when the container has hung, so it never restarts it.",
                  "fields": ["livenessProbe"]},
    "CKV_K8S_9": {"name": "No readiness probe", "why": "Traffic can be sent to the container before it is ready to serve.",
                  "fields": ["readinessProbe"]},
    "CKV_K8S_10": {"name": "No CPU request", "why": "The scheduler does not know how much CPU to reserve for the container.",
                   "fields": ["requests", "cpu"]},
    "CKV_K8S_11": {"name": "No CPU limit", "why": "One container can take all the CPU on its node and starve the others.",
                   "fields": ["limits", "cpu"]},
    "CKV_K8S_12": {"name": "No memory request", "why": "The scheduler does not know how much memory to reserve for the container.",
                   "fields": ["requests", "memory"]},
    "CKV_K8S_13": {"name": "No memory limit", "why": "One container can use all the memory on its node and get others killed.",
                   "fields": ["limits", "memory"]},
    "CKV_K8S_15": {"name": "Image pull policy is not Always", "why": "A node may run an old cached copy of the image instead of the current one.",
                   "fields": ["imagePullPolicy"]},
    "CKV_K8S_20": {"name": "Privilege escalation allowed", "why": "A process in the container could gain more rights than it started with.",
                   "fields": ["allowPrivilegeEscalation"]},
    "CKV_K8S_22": {"name": "Root filesystem is writable", "why": "An attacker who gets in can change files inside the container.",
                   "fields": ["readOnlyRootFilesystem"]},
    "CKV_K8S_23": {"name": "Container may run as root", "why": "A break-out from a root container gives root on the node.",
                   "fields": ["runAsNonRoot", "runAsUser"]},
    "CKV_K8S_28": {"name": "NET_RAW capability not dropped", "why": "The container can craft raw network packets, which enables spoofing.",
                   "fields": ["capabilities", "drop", "NET_RAW", "ALL"]},
    "CKV_K8S_29": {"name": "No pod security settings", "why": "Without a securityContext the pod runs with the permissive defaults.",
                   "fields": ["securityContext"]},
    "CKV_K8S_30": {"name": "No container security settings", "why": "Without a securityContext the container runs with the permissive defaults.",
                   "fields": ["securityContext"]},
    "CKV_K8S_31": {"name": "No seccomp profile", "why": "The container may make any system call; the default profile blocks the risky ones.",
                   "fields": ["seccompProfile", "RuntimeDefault"]},
    "CKV_K8S_37": {"name": "Linux capabilities not dropped", "why": "The container keeps kernel privileges it does not need.",
                   "fields": ["capabilities", "drop", "ALL"]},
    "CKV_K8S_38": {"name": "Service-account token mounted", "why": "A token for the Kubernetes API sits in the container; if it leaks, the API can be called with it.",
                   "fields": ["automountServiceAccountToken"]},
    "CKV_K8S_40": {"name": "User ID too low", "why": "A low user ID inside the container can match a real user on the host.",
                   "fields": ["runAsUser"]},
    "OPA_NO_ROOT": {"name": "Container may run as root", "why": "Team rule: every container must declare that it does not run as root.",
                    "fields": ["runAsNonRoot"]},
    "OPA_READONLY_ROOTFS": {"name": "Root filesystem is writable", "why": "Team rule for production: containers must not be able to change their own files.",
                            "fields": ["readOnlyRootFilesystem"]},
    "OPA_REQUIRE_CPU_LIMIT": {"name": "No CPU limit", "why": "Team rule: every container must have a CPU limit.",
                              "fields": ["limits", "cpu"]},
    "OPA_REQUIRE_MEM_LIMIT": {"name": "No memory limit", "why": "Team rule: every container must have a memory limit.",
                              "fields": ["limits", "memory"]},
    "OPA_REQUIRE_READINESS_PROBE": {"name": "No readiness probe", "why": "Team rule for staging and above: traffic must wait until the container is ready.",
                                    "fields": ["readinessProbe"]},
    "PLAN_CONFORMANCE_IMAGE": {"name": "Image differs from the plan", "why": "The file must use exactly the image the user approved.",
                               "fields": ["image"]},
    "PLAN_CONFORMANCE_MISSING": {"name": "Planned resource is missing", "why": "Something the user approved was not generated.",
                                 "fields": []},
    "PLAN_CONFORMANCE_UNPLANNED": {"name": "Resource is not in the plan", "why": "The files contain something the user did not approve.",
                                   "fields": []},
}


def rule_info(rule_id: str, message: Optional[str]) -> dict:
    """Plain-English name and why it matters. For an unknown rule: the checker's own message, and no explanation."""
    known = RULES.get(rule_id)
    if known:
        return {"name": known["name"], "why": known["why"], "fields": list(known["fields"]), "known": True}
    return {"name": (message or rule_id).strip() or rule_id, "why": "", "fields": [], "known": False}


def _yaml_key(line: str) -> str:
    """The key of a YAML line ('- name: x' -> 'name'), or the whole line if it has none."""
    text = line.strip().lstrip("- ").strip()
    return text.split(":", 1)[0].strip() if ":" in text else text


def fix_evidence(before: str, after: str, fields: List[str], limit: int = 3) -> List[str]:
    """Changed lines of the real diff that mention one of the rule's fields.

    A removed line and an added line are joined as 'old → new' only when they set the
    same YAML key; otherwise each is shown on its own as 'added:' or 'removed:'. The
    text is copied from the two drafts and nothing else. An empty list means no
    changed line mentions the rule's fields (or the rule has none), and the caller
    must say so instead of guessing.
    """
    if not fields:
        return []
    removed, added = [], []
    for row in diff_rows(before, after):
        if row["tag"] in ("removed", "changed") and row["left"].strip():
            removed.append(row["left"].strip())
        if row["tag"] in ("added", "changed") and row["right"].strip():
            added.append(row["right"].strip())
    relevant = lambda text: any(field in text for field in fields)  # noqa: E731
    lines, used = [], set()
    for old in removed:
        if not relevant(old):
            continue
        match = next((i for i, new in enumerate(added) if i not in used and _yaml_key(new) == _yaml_key(old)), None)
        if match is None:
            lines.append(f"removed: {old}")
        else:
            used.add(match)
            lines.append(f"{old} → {added[match]}")
    lines += [f"added: {new}" for i, new in enumerate(added) if i not in used and relevant(new)]
    lines = list(dict.fromkeys(lines))  # the same edit in two containers is shown once
    if len(lines) > limit:
        lines = lines[:limit] + [f"and {len(lines) - limit} more changed line(s)"]
    return lines


def violation_cards(rounds: List[dict], number: int) -> List[dict]:
    """One card per violation for a round: the rule in plain words, what was wrong, and what the fix changed."""
    cards = []
    for row in violation_rows(rounds, number):
        info = rule_info(row["rule_id"], row.get("message"))
        card = dict(row, name=info["name"], why=info["why"], known=info["known"], evidence=[], evidence_note="")
        filename = row.get("file")
        if row["status"] == "fixed in this round":
            before = rounds[number - 1]["files"].get(filename, "") if filename else ""
            after = rounds[number]["files"].get(filename, "") if filename else ""
            card["evidence"] = fix_evidence(before, after, info["fields"])
            if not card["evidence"]:
                changed = diff_stats(diff_rows(before, after)) if filename else (0, 0)
                card["evidence_note"] = (
                    f"The rule no longer fails. See the diff of {filename} above ({changed[0]} line(s) removed, {changed[1]} added)."
                    if filename and sum(changed) else "The rule no longer fails; this file's text did not change.")
        elif row["status"] == "still failing":
            card["evidence_note"] = "Not fixed by this round. It goes back to the model with the failed attempt."
        else:
            card["evidence_note"] = ("Found in the first draft." if number == 0 else "Introduced by this round's fix.") + \
                " Select the next round to see how it was fixed."
        cards.append(card)
    return cards


PIPELINE = [("guardrails", "Guardrails"), ("plan", "Plan"), ("approve", "Approve"), ("generate", "Generate"),
            ("gauntlet", "Gauntlet: 4 checkers"), ("fix", "Fix"), ("pull_request", "Pull request"), ("argocd", "ArgoCD")]


def count_word(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def pipeline_stages(log: Optional[dict], awaiting_approval: bool = False, current: Optional[str] = None) -> dict:
    """The eight pipeline boxes for one run, each with a state and one line of real data.

    States: 'done' (ran and completed), 'failed' (where the run stopped), 'current',
    'skipped' (did not run, by design), 'pending' (not reached), 'idle' (no run yet),
    'not_built' (pull request and ArgoCD). Also returns the loop label.

    `current` is for a run in progress: the stage being worked on right now. It is
    shown as 'current', the stages before it as done, and the ones after as pending.
    """
    stages = {key: {"key": key, "label": label, "state": "idle" if not log else "pending", "note": ""} for key, label in PIPELINE}
    for key in ("pull_request", "argocd"):
        stages[key].update(state="not_built", note="not built yet")
    if not log:
        return {"stages": list(stages.values()), "loop": "", "looped": False}

    def done(*keys):
        for key in keys:
            stages[key]["state"] = "done"

    status = log.get("status") or "running"
    reason = (log.get("rejection_reason") or "").split(":")[0]
    rounds = log.get("rounds") or []
    plan = log.get("approved_plan") or log.get("plan")
    iterations = log.get("iterations") or 0

    # Guardrails
    if log.get("guardrails_bypassed"):
        stages["guardrails"].update(state="skipped", note="bypassed (benchmark)")
    elif status == "rejected" and reason != "plan_not_approved":
        stages["guardrails"].update(state="failed", note="refused")
    else:
        stages["guardrails"].update(state="done", note="passed")
    refused = stages["guardrails"]["state"] == "failed"

    # Plan and approval
    if plan and not refused:
        done("plan")
        stages["plan"]["note"] = count_word(len(plan["resources"]), "resource")
        if log.get("plan_source") == "shared":
            stages["plan"]["note"] += ", shared plan"
        if log.get("approved_plan"):
            done("approve")
            stages["approve"]["note"] = "auto-approved" if log.get("benchmark") else "approved"
        elif reason == "plan_not_approved":
            stages["approve"].update(state="failed", note="declined")
        elif awaiting_approval:
            stages["approve"].update(state="current", note="waiting for you")
    elif not refused and status == "escalated":
        stages["plan"].update(state="failed", note="no usable plan")

    # Generate, Gauntlet, Fix
    if rounds:
        done("generate")
        stages["generate"]["note"] = count_word(len(rounds[0]["files"]), "file")
        first, last = len(rounds[0]["violations"]), len(rounds[-1]["violations"])
        stages["gauntlet"]["note"] = (f"{first} → {last} violations" if len(rounds) > 1 else count_word(first, "violation"))
        fix_failed = status == "escalated" and iterations > rounds[-1]["round"]
        if status == "passed":
            done("gauntlet")
        elif status == "escalated" and not fix_failed:
            stages["gauntlet"]["state"] = "failed"
        else:
            done("gauntlet")
        if fix_failed:
            stages["fix"].update(state="failed", note=f"round {iterations} did not finish")
        elif iterations:
            done("fix")
            stages["fix"]["note"] = count_word(iterations, "round")
        elif log.get("arm") in ("A", "B"):
            stages["fix"].update(state="skipped", note="no fix loop in this arm")
        elif status == "passed":
            stages["fix"].update(state="skipped", note="not needed")
    elif log.get("approved_plan") and status == "escalated":
        stages["generate"].update(state="failed", note="no usable files")

    if current in _LIVE_ORDER:
        position = _LIVE_ORDER.index(current)
        for index, key in enumerate(_LIVE_ORDER):
            stage = stages[key]
            if key == current:
                stage.update(state="current", note=stage["note"] if key == "gauntlet" and rounds else "in progress")
            elif index < position:
                if stage["state"] != "skipped":
                    stage["state"] = "done"
            elif key == "fix" and current == "gauntlet" and iterations:
                stage["state"] = "done"       # the loop: this Gauntlet run follows a fix round
            else:
                stage.update(state="pending", note="")

    looped = iterations > 0
    loop = f"looped {iterations} time{'' if iterations == 1 else 's'}" if looped else ""
    return {"stages": list(stages.values()), "loop": loop, "looped": looped}


_LIVE_ORDER = ["guardrails", "plan", "approve", "generate", "gauntlet", "fix"]


def live_position(step_name: str, log: dict) -> Tuple[Optional[str], str]:
    """After graph node `step_name` has finished: (the stage now in progress, a one-line status).

    The stage is None once nothing is left to run.
    """
    status = log.get("status") or "running"
    iterations = log.get("iterations") or 0
    if status != "running":
        if status == "passed" and step_name in ("gauntlet", "pull_request"):
            return None, "Passed the Gauntlet. Finishing."
        return None, f"{status.capitalize()}. Finishing."
    if step_name in ("guardrails", "plan", "approve"):
        return "generate", "Draft 1: calling the model, one call per resource"
    if step_name == "generate":
        return "gauntlet", "Gauntlet: checking draft 1 with 4 checkers"
    if step_name == "fix":
        return "gauntlet", f"Gauntlet: checking fix {iterations} with 4 checkers"
    if step_name == "gauntlet":
        failing = len((log.get("rounds") or [{}])[-1].get("violations") or [])
        return "fix", f"Fix round {iterations + 1}: sending {count_word(failing, 'violation')} back to the model"
    return None, "Finishing."


def node_settled(step_name: str, log: dict, before: dict) -> bool:
    """True once `log` shows what graph node `step_name` did, compared with the log from before it ran."""
    if (log.get("status") or "running") != "running":
        return True
    if step_name == "generate":
        return bool(log.get("files"))
    if step_name == "gauntlet":
        return len(log.get("rounds") or []) > len(before.get("rounds") or [])
    if step_name == "fix":
        return (log.get("iterations") or 0) > (before.get("iterations") or 0)
    if step_name == "approve":
        return bool(log.get("approved_plan"))
    return True


def wait_for_node(read_log, step_name: str, before: dict, timeout: float = 2.0, pause=None) -> dict:
    """The run log as it stands once node `step_name` has been recorded.

    The pipeline reports a finished node a moment before the saved state includes
    it, so reading the state straight away can return the previous step. This
    re-reads until the node's effect is visible, or gives up after `timeout`
    seconds and returns the latest state it has.
    """
    import time
    pause = pause or time.sleep
    deadline = time.time() + timeout
    log = read_log()
    while not node_settled(step_name, log, before) and time.time() < deadline:
        pause(0.02)
        log = read_log()
    return log


def call_status(stage: str, context: dict) -> str:
    """Status line for one model call, from the stage and the context the LLM service logs with it."""
    target = f" for {context['file']}" if context.get("file") else ""
    if stage == "generate":
        return f"Draft 1: calling the model{target}"
    if stage == "fix":
        return f"Fix round {context.get('round', '?')}: calling the model{target}"
    return f"{stage.capitalize()}: calling the model{target}"


def retry_status(wait_seconds: float, last_call: Optional[dict]) -> str:
    """Status line while the LLM service waits before trying a call again."""
    seconds = f"{wait_seconds:.0f} s"
    if (last_call or {}).get("http_status") == 429:
        return f"Rate limited, retrying in {seconds}"
    return f"Model call failed, retrying in {seconds}"


class watch_llm:
    """Reports what the LLM service is doing while a live run is in progress.

    For the length of the `with` block it wraps the service's `_call` (one model
    call) and `sleep` (a wait before a retry) on this one object so each reports a
    status line first, then puts the originals back. The service's behaviour and
    its log are unchanged. A service without these (the mock) is left alone.
    """
    def __init__(self, llm, on_status):
        self.llm, self.on_status, self.saved = llm, on_status, {}

    def __enter__(self):
        llm, report = self.llm, self.on_status
        if callable(getattr(llm, "_call", None)):
            original_call = llm._call
            self.saved["_call"] = ("_call" in vars(llm), vars(llm).get("_call"))

            def call(prompt, stage, **context):
                report(call_status(stage, context))
                return original_call(prompt, stage, **context)
            llm._call = call
        if callable(getattr(llm, "sleep", None)):
            original_sleep = llm.sleep
            self.saved["sleep"] = ("sleep" in vars(llm), vars(llm).get("sleep"))

            def sleep(seconds):
                log = getattr(llm, "call_log", None) or [None]
                report(retry_status(seconds, log[-1]))
                return original_sleep(seconds)
            llm.sleep = sleep
        return self

    def __exit__(self, *exc):
        for name, (was_set, value) in self.saved.items():
            if was_set:
                setattr(self.llm, name, value)
            else:
                delattr(self.llm, name)
        return False


def story(log: Optional[dict], awaiting_approval: bool = False) -> str:
    """One or two sentences that say what happened in this run, built only from the log."""
    if not log:
        return "Nothing has run yet. A request goes through these stages from left to right."
    status = log.get("status") or "running"
    reason_code = (log.get("rejection_reason") or "").split(":")[0]
    reason_text = plain_reason(log.get("rejection_reason")).rstrip(".")
    rounds = log.get("rounds") or []
    iterations = log.get("iterations") or 0

    if status == "rejected":
        if reason_code == "plan_not_approved":
            return "The plan was declined, so nothing was generated."
        return f"Guardrails refused this request before any model call. Reason: {reason_text}."
    if status == "running":
        return ("The plan is ready. Nothing is generated until it is approved." if awaiting_approval
                else "This run is in progress.")
    if not rounds:
        return f"The run stopped before any file was generated and was escalated to a person. Reason: {reason_text}."

    first = rounds[0]["violations"]
    rules = len({v["rule_id"] for v in first})
    safety = all(v.get("tool") != "plan" for v in first)
    broke = (f"The first draft broke {rules} {'safety ' if safety else ''}rule{'' if rules == 1 else 's'}"
             if rules else "The first draft broke no rules")
    remaining = len(rounds[-1]["violations"])

    if log.get("arm") in ("A", "B"):
        outcome = "It passed all 4 checks." if status == "passed" else f"{count_word(remaining, 'violation')} remain, with no loop to fix them."
        return f"Benchmark arm {log['arm']}: one model call and no fix loop. {broke}. {outcome}"
    if status == "passed":
        if iterations == 0:
            return "The first draft broke no rules. All 4 checkers passed it, so no fix round was needed."
        caught = "it" if rules == 1 else "both" if rules == 2 else f"all {rules}"
        fixed = "it" if rules == 1 else "them"
        return (f"{broke}. The checkers caught {caught}, the model fixed {fixed} in {count_word(iterations, 'round')}, "
                "and the final files passed all 4 checks.")
    if reason_code == "max_fix_rounds":
        return (f"{broke}. After {iterations} fix rounds {count_word(remaining, 'violation')} still remained, "
                "so the run was escalated to a person and nothing was published.")
    if reason_code == "tool_crash":
        return ("A checker failed to run, so the run was escalated to a person. "
                "A checker that cannot run is never counted as a pass.")
    return f"{broke}. The run was then escalated to a person. Reason: {reason_text}."
