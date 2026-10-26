"""Builds the NL2Infra RAG knowledge base from three local sources:

  1. the OPA/Rego policy rules (their plain-English form, with rule ids),
  2. the Checkov rule list, plus a short company standards document, and
  3. past *approved* manifests taken from the run logs.

Each document is a dict: {"id", "text", "source", "metadata"}. No network, no secrets.
"""
import glob
import json
import os
from typing import Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
STANDARDS_DOC = os.path.join(ROOT, "docs", "standards.md")

# Short keyword hints per OPA rule, so a user's everyday wording ("root", "latest", "cpu")
# retrieves the right policy even when the rule text uses camelCase field names.
_OPA_KEYWORDS = {
    "OPA_NO_ROOT": "run as root non-root runAsNonRoot user security context",
    "OPA_NO_PRIVILEGED": "privileged container escalation root",
    "OPA_NO_PLAINTEXT_SECRET": "plaintext password token api key secret env var credentials",
    "OPA_REQUIRE_CPU_LIMIT": "cpu limit resources compute throttle",
    "OPA_REQUIRE_MEM_LIMIT": "memory limit resources ram oom",
    "OPA_REQUIRE_READINESS_PROBE": "readiness probe health check traffic ready",
    "OPA_DISALLOW_LATEST_TAG": "image latest tag version pin mutable",
    "OPA_PRODUCTION_REPLICAS": "replicas availability production high availability two",
    "OPA_NO_DEFAULT_NAMESPACE": "default namespace isolation environment",
    "OPA_READONLY_ROOTFS": "read only root filesystem immutable writable",
}


def _policy_documents() -> List[dict]:
    from rules import OPA_RULES  # plain-English OPA rules keyed by pack
    docs = []
    for pack, rules in OPA_RULES.items():
        for rule_id, description in rules.items():
            keywords = _OPA_KEYWORDS.get(rule_id, "")
            docs.append({
                "id": f"policy:{rule_id}",
                "text": f"{rule_id} ({pack} policy): {description} Keywords: {keywords}",
                "source": "policy",
                "metadata": {"rule_id": rule_id, "pack": pack},
            })
    return docs


def _checkov_documents() -> List[dict]:
    from rules import RULE_LIST_FILE
    docs = []
    if not os.path.exists(RULE_LIST_FILE):
        return docs
    with open(RULE_LIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rule_id = line.split(":", 1)[0].strip()
            docs.append({
                "id": f"checkov:{rule_id}",
                "text": f"{line} (Checkov rule)",
                "source": "checkov",
                "metadata": {"rule_id": rule_id},
            })
    return docs


def _standards_documents(path: str = STANDARDS_DOC) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # One document per "## section" so retrieval returns a focused chunk.
    docs = []
    current_title, buffer = "intro", []
    sections = []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((current_title, "\n".join(buffer)))
            current_title, buffer = line[3:].strip(), []
        else:
            buffer.append(line)
    sections.append((current_title, "\n".join(buffer)))
    for i, (title, body) in enumerate(sections):
        if body.strip():
            docs.append({
                "id": f"standards:{i}:{title}",
                "text": f"Company standard — {title}: {body.strip()}",
                "source": "standards",
                "metadata": {"section": title},
            })
    return docs


def _example_documents(runs_dir: str, limit: int = 40) -> List[dict]:
    """Manifests from past runs that passed the Gauntlet — good few-shot examples."""
    docs = []
    if not runs_dir or not os.path.isdir(runs_dir):
        return docs
    paths = sorted(glob.glob(os.path.join(runs_dir, "*.json")))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                log = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if log.get("status") != "passed" and log.get("final_status") != "passed":
            continue
        files = (log.get("files") or {})
        if not files and log.get("rounds"):
            files = log["rounds"][-1].get("files", {})  # final, passing draft
        for filename, yaml_text in files.items():
            if not isinstance(yaml_text, str) or not yaml_text.strip():
                continue
            docs.append({
                "id": f"example:{log.get('request_id')}:{filename}",
                "text": f"Approved example manifest {filename}:\n{yaml_text.strip()}",
                "source": "example",
                "metadata": {"request_id": log.get("request_id"), "filename": filename},
            })
            if len(docs) >= limit:
                return docs
    return docs


def build_documents(runs_dir: Optional[str] = "runs", include_examples: bool = True) -> List[dict]:
    """All knowledge-base documents: policies + checkov + standards + approved examples."""
    docs = _policy_documents() + _checkov_documents() + _standards_documents()
    if include_examples:
        docs += _example_documents(runs_dir)
    return docs


def build_runlog_documents(runs_dir: str = "runs") -> List[dict]:
    """One document per run, summarising what happened — the corpus for the 'why?' bot."""
    docs = []
    if not runs_dir or not os.path.isdir(runs_dir):
        return docs
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                log = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        rid = log.get("request_id")
        prompt = log.get("prompt") or log.get("user_prompt") or ""
        status = log.get("status") or log.get("final_status") or ""
        reason = log.get("rejection_reason") or ""
        rounds = log.get("rounds") or []
        rule_ids = sorted({v.get("rule_id") for r in rounds for v in r.get("violations", []) if v.get("rule_id")})
        fixed = []
        if len(rounds) >= 2:
            first = {v.get("rule_id") for v in rounds[0].get("violations", [])}
            last = {v.get("rule_id") for v in rounds[-1].get("violations", [])}
            fixed = sorted(first - last)
        text = (f"Run {rid}. Request: {prompt}. Final status: {status}. "
                f"{('Reason: ' + reason + '. ') if reason else ''}"
                f"Fix rounds: {log.get('iterations', 0)}. "
                f"Violations seen: {', '.join(rule_ids) or 'none'}. "
                f"Fixed during the loop: {', '.join(fixed) or 'none'}.")
        docs.append({
            "id": f"run:{rid}",
            "text": text,
            "source": "runlog",
            "metadata": {"request_id": rid, "status": status, "reason": reason,
                         "rule_ids": rule_ids, "fixed": fixed, "prompt": prompt},
        })
    return docs
