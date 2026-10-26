"""A small, offline retriever over the knowledge base.

Embeddings default to scikit-learn TF-IDF (no model download, deterministic, CI-friendly);
the index is FAISS inner-product over L2-normalised vectors when FAISS is installed, otherwise
a numpy cosine fallback. Either way the API is the same.

Set NL2INFRA_EMBEDDINGS=hf to use sentence-transformers instead (downloads a model).
"""
import os
import re
from typing import List, Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


def _split_identifiers(text: str) -> str:
    """Split camelCase and symbol-joined identifiers so field names match plain words.

    'runAsNonRoot', 'readOnlyRootFilesystem' -> 'run as non root', 'read only root filesystem'.
    """
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_\-./:]+", " ", text)
    return text.lower()


class _TfidfBackend:
    def __init__(self, texts: List[str]):
        self.vectorizer = TfidfVectorizer(preprocessor=_split_identifiers, stop_words="english")
        matrix = self.vectorizer.fit_transform(texts).astype(np.float32).toarray()
        self.matrix = _normalize(matrix)

    def embed(self, text: str) -> np.ndarray:
        vec = self.vectorizer.transform([text]).astype(np.float32).toarray()
        return _normalize(vec)


class _HFBackend:  # pragma: no cover - exercised only when opted in
    def __init__(self, texts: List[str]):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(os.environ.get("NL2INFRA_HF_MODEL", "all-MiniLM-L6-v2"))
        self.matrix = _normalize(np.asarray(self.model.encode(texts), dtype=np.float32))

    def embed(self, text: str) -> np.ndarray:
        return _normalize(np.asarray(self.model.encode([text]), dtype=np.float32))


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class Retriever:
    """Holds the documents and a similarity index over their text."""

    def __init__(self, documents: List[dict]):
        if not documents:
            raise ValueError("cannot build a retriever over an empty document set")
        self.documents = documents
        texts = [d["text"] for d in documents]
        backend = os.environ.get("NL2INFRA_EMBEDDINGS", "tfidf").lower()
        self.backend = _HFBackend(texts) if backend == "hf" else _TfidfBackend(texts)
        self._index = self._build_faiss(self.backend.matrix)

    @staticmethod
    def _build_faiss(matrix: np.ndarray):
        try:
            import faiss
            index = faiss.IndexFlatIP(matrix.shape[1])
            index.add(np.ascontiguousarray(matrix))
            return index
        except Exception:
            return None  # fall back to numpy cosine in search()

    def search(self, query: str, k: int = 5, sources: Optional[List[str]] = None) -> List[dict]:
        """Top-k documents for the query, each with a similarity 'score'. `sources` filters by type."""
        q = self.backend.embed(query)
        if self._index is not None:
            scores, idx = self._index.search(np.ascontiguousarray(q), min(k * 4, len(self.documents)))
            pairs = list(zip(idx[0].tolist(), scores[0].tolist()))
        else:
            sims = (self.backend.matrix @ q[0])
            order = np.argsort(-sims)[: k * 4]
            pairs = [(int(i), float(sims[i])) for i in order]
        results = []
        for i, score in pairs:
            if i < 0:
                continue
            doc = self.documents[i]
            if sources and doc["source"] not in sources:
                continue
            results.append({**doc, "score": round(float(score), 4)})
            if len(results) >= k:
                break
        return results

    def context_for_resource(self, resource, k: int = 5) -> str:
        """A grounding block for the generate prompt: the most relevant policies and one example."""
        images = getattr(resource, "planned_images", lambda: [])()
        query = f"{resource.type} {resource.name} namespace {resource.namespace} image {' '.join(images)}"
        rules = self.search(query, k=k, sources=["policy", "checkov", "standards"])
        examples = self.search(f"{resource.type} manifest {' '.join(images)}", k=1, sources=["example"])
        lines = []
        for r in rules:
            snippet = r["text"].split("\n", 1)[0][:200]
            lines.append(f"- {snippet}")
        for e in examples:
            lines.append("- Example of a similar approved manifest:\n" + e["text"][:600])
        return "\n".join(lines)


_cache = {}


def get_retriever(runs_dir: str = "runs", include_examples: bool = True) -> Optional["Retriever"]:
    """A cached retriever over the full knowledge base. Returns None if it cannot be built."""
    from rag.knowledge_base import build_documents
    key = (runs_dir, include_examples, os.environ.get("NL2INFRA_EMBEDDINGS", "tfidf"))
    if key not in _cache:
        try:
            _cache[key] = Retriever(build_documents(runs_dir, include_examples))
        except Exception:
            _cache[key] = None
    return _cache[key]


def clear_cache() -> None:
    _cache.clear()
