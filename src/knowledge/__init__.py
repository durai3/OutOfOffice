"""
DELEG8 dynamic knowledge base: local documents -> ChromaDB -> grounded answers.

Public surface:
    - :mod:`src.knowledge.config`     KnowledgeSettings from the environment.
    - :mod:`src.knowledge.store`      Persistent Chroma collection + CPU embedder.
    - :mod:`src.knowledge.ingest`     Load, chunk, embed and store documents (also a CLI;
                                      import it explicitly: ``from src.knowledge.ingest import DocumentIngestor``).
    - :mod:`src.knowledge.retriever`  Threshold-gated retrieval, UNKNOWN_CONTEXT, brain adapter.
"""

from src.knowledge.config import KnowledgeConfigError, KnowledgeSettings
from src.knowledge.retriever import (
    UNKNOWN_CONTEXT,
    AnswerComposer,
    DocumentKnowledge,
    KnowledgeRetriever,
    RetrievalResult,
    RetrievalStatus,
    RetrievedChunk,
    build_knowledge_source,
)
from src.knowledge.store import KnowledgeStore

__all__ = [
    "AnswerComposer",
    "DocumentKnowledge",
    "KnowledgeConfigError",
    "KnowledgeRetriever",
    "KnowledgeSettings",
    "KnowledgeStore",
    "RetrievalResult",
    "RetrievalStatus",
    "RetrievedChunk",
    "UNKNOWN_CONTEXT",
    "build_knowledge_source",
]
