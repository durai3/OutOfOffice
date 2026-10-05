"""
Configuration for the document knowledge base, read from the environment.

Paths may be absolute or relative to the project root (the directory that
holds ``src/``), so the defaults work no matter where a script is launched.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DEFAULT_EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
"""384-dim sentence-transformers model; ~90 MB, ~10 ms per query on a laptop CPU."""


class KnowledgeConfigError(RuntimeError):
    """Raised when a knowledge-base setting is malformed."""


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise KnowledgeConfigError(f"{name} must be an integer (got {raw!r}).") from exc
    if value < minimum:
        raise KnowledgeConfigError(f"{name} must be >= {minimum} (got {value}).")
    return value


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise KnowledgeConfigError(f"{name} must be a number (got {raw!r}).") from exc
    if not low <= value <= high:
        raise KnowledgeConfigError(f"{name} must be between {low} and {high} (got {value}).")
    return value


def _resolve(path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate).resolve()


@dataclass(frozen=True)
class KnowledgeSettings:
    """All knobs for ingestion and retrieval. Frozen so components can share it."""

    documents_dir: Path = PROJECT_ROOT / "data" / "documents"
    """Folder scanned by the ingestor for .md / .txt / .pdf files."""
    chroma_dir: Path = PROJECT_ROOT / "data" / "chroma_db"
    """ChromaDB persistence directory."""
    collection_name: str = "deleg8_documents"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    min_similarity: float = 0.30
    """Cosine similarity a passage must reach to count as relevant context.

    Calibrated on all-MiniLM-L6-v2: unrelated questions score below ~0.26
    against project documents, relevant ones above ~0.35.
    """
    top_k: int = 4
    """Passages fetched per query before the threshold is applied."""
    chunk_size: int = 800
    """Characters per chunk handed to the embedder."""
    chunk_overlap: int = 120
    max_context_documents: int = 3
    """Cap on distinct source documents whose FULL text is loaded for the LLM answer
    synthesiser, even if more chunks clear the similarity threshold. Chunks/threshold
    still decide *which* documents are relevant; this only bounds how many full
    documents one answer call can pull in.
    """
    max_document_context_chars: int = 300_000
    """Cap on total characters of full document text handed to the LLM answer
    synthesiser per question. Generous relative to modern ~1M-token context windows;
    guards only against a pathologically large upload.
    """

    @classmethod
    def for_tenant(
        cls, documents_dir: Path, collection_name: str, chroma_dir: Path | None = None
    ) -> KnowledgeSettings:
        """Settings for one tenant: their own documents folder, their own collection.

        Every tuning knob (thresholds, chunk sizes, the embedding model) still
        comes from the environment, because those are deployment decisions
        rather than per-user ones; only the two fields that decide *whose data
        this is* are overridden.
        """
        base = cls.from_env()
        return dataclasses.replace(
            base,
            documents_dir=documents_dir,
            collection_name=collection_name,
            chroma_dir=chroma_dir or base.chroma_dir,
        )

    @classmethod
    def from_env(cls) -> KnowledgeSettings:
        """Build settings from ``DELEG8_*`` variables, falling back to the defaults above.

        Raises:
            KnowledgeConfigError: on a malformed value.
        """
        chunk_size = _env_int("DELEG8_CHUNK_SIZE", 800, minimum=100)
        chunk_overlap = _env_int("DELEG8_CHUNK_OVERLAP", 120, minimum=0)
        if chunk_overlap >= chunk_size:
            raise KnowledgeConfigError("DELEG8_CHUNK_OVERLAP must be smaller than DELEG8_CHUNK_SIZE.")
        return cls(
            documents_dir=_resolve(_env_str("DELEG8_DOCUMENTS_DIR", "data/documents")),
            chroma_dir=_resolve(_env_str("DELEG8_CHROMA_DIR", "data/chroma_db")),
            collection_name=_env_str("DELEG8_RAG_COLLECTION", "deleg8_documents"),
            embedding_model=_env_str("DELEG8_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            min_similarity=_env_float("DELEG8_RAG_MIN_SIMILARITY", 0.30, low=0.0, high=1.0),
            top_k=_env_int("DELEG8_RAG_TOP_K", 4, minimum=1),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_context_documents=_env_int("DELEG8_MAX_CONTEXT_DOCUMENTS", 3, minimum=1),
            max_document_context_chars=_env_int("DELEG8_MAX_DOCUMENT_CONTEXT_CHARS", 300_000, minimum=1000),
        )
