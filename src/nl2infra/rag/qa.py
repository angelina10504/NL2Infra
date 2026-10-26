"""A small retrieval Q&A: "Why was my request blocked or changed?"

Retrieves the most relevant run logs for the question and composes a grounded, deterministic
answer that cites rule ids and the recorded reason. No LLM call, so it is free and offline;
the same retrieval machinery (TF-IDF + FAISS) powers it as the generator grounding.
"""
from typing import Optional

from rag.knowledge_base import build_runlog_documents
from rag.retriever import Retriever


def answer_why(question: str, runs_dir: str = "runs", k: int = 3) -> dict:
    """Return {'answer', 'sources'} explaining the most relevant past runs."""
    docs = build_runlog_documents(runs_dir)
    if not docs:
        return {"answer": "There are no run logs yet, so there is nothing to explain.", "sources": []}

    retriever = Retriever(docs)
    hits = retriever.search(question, k=k, sources=["runlog"])
    if not hits:
        return {"answer": "No past run matched that question.", "sources": []}

    lines = []
    for hit in hits:
        meta = hit["metadata"]
        rid, status, reason = meta.get("request_id"), meta.get("status"), meta.get("reason")
        if status == "rejected":
            lines.append(f"Run {rid} was blocked by the guardrails: {reason}.")
        elif status == "escalated":
            rules = ", ".join(meta.get("rule_ids") or []) or "unresolved violations"
            lines.append(f"Run {rid} could not be made compliant ({reason or 'escalated'}); "
                         f"the Gauntlet still reported: {rules}.")
        elif status == "passed":
            fixed = ", ".join(meta.get("fixed") or [])
            if fixed:
                lines.append(f"Run {rid} passed after the fix loop changed the manifests to satisfy: {fixed}.")
            else:
                lines.append(f"Run {rid} passed the Gauntlet on the first draft; nothing had to change.")
        else:
            lines.append(f"Run {rid} ended with status '{status}'.")

    return {
        "answer": " ".join(lines),
        "sources": [{"request_id": h["metadata"].get("request_id"),
                     "status": h["metadata"].get("status"),
                     "score": h["score"]} for h in hits],
    }
