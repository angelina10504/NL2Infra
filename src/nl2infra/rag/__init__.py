"""Retrieval-augmented generation over NL2Infra's own policies, standards and run logs.

- `knowledge_base` builds documents from Rego/Checkov rules, the standards doc and approved
  manifests; `retriever` indexes them (TF-IDF + FAISS, offline) and grounds the generate prompt;
  `qa` answers "why was my request blocked or changed?" over the run logs.
"""
from rag.knowledge_base import build_documents, build_runlog_documents
from rag.retriever import Retriever, get_retriever, clear_cache
from rag.qa import answer_why

__all__ = [
    "build_documents", "build_runlog_documents",
    "Retriever", "get_retriever", "clear_cache", "answer_why",
]
