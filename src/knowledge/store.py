"""
The local vector store: ChromaDB persisted on disk, embeddings computed on CPU.

:class:`KnowledgeStore` owns three things and hands them to the ingestor and
the retriever:

* the sentence-transformers embedding function (``all-MiniLM-L6-v2`` by
  default, normalised so cosine distance is ``1 - dot``);
* one persistent Chroma collection configured for cosine space;
* a single worker thread. Embedding is CPU-bound and Chroma's local client
  is synchronous, so every call from the event loop goes through
  :meth:`run`, which serialises access and keeps the loop free.

The model is loaded lazily on first use (a few seconds; the first ever run
also downloads it from Hugging Face). Call :meth:`open` or :meth:`open_async`
at startup to pay that cost before the meeting starts.

Multi-tenant note
-----------------
Each user's documents live in their *own* Chroma collection - that is what
makes one tenant's knowledge unreachable from another's retriever. What they
must not each own is the embedding model: ``all-MiniLM-L6-v2`` is ~90 MB of
resident memory and several seconds to load, so twenty users would mean
twenty copies and twenty cold starts.

:class:`_SharedBackend` is the fix. One client, one embedder and one worker
thread per (directory, model) pair, shared by every :class:`KnowledgeStore`
pointed at them; the store itself still owns nothing but a collection name.
Isolation is unaffected - a collection is only reachable through the store
that names it - while the cost of the model is paid once per process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

# Quiet the ML stack before it is imported: no tokenizer fork warnings, no
# download progress bars in meeting logs, no Chroma telemetry.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

import warnings  # noqa: E402

warnings.filterwarnings("ignore", message=r"Warning: You are sending unauthenticated requests")
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

import chromadb  # noqa: E402
from chromadb.api.models.Collection import Collection  # noqa: E402
from chromadb.config import Settings as ChromaSettings  # noqa: E402
from chromadb.utils.embedding_functions import (
    SentenceTransformerEmbeddingFunction,  # noqa: E402
)

from src.knowledge.config import KnowledgeSettings  # noqa: E402

logger = logging.getLogger("deleg8.knowledge.store")

T = TypeVar("T")


_EMBEDDERS: dict[str, SentenceTransformerEmbeddingFunction] = {}
_EMBEDDER_LOCK = threading.RLock()


def shared_embedder(model_name: str) -> SentenceTransformerEmbeddingFunction:
    """The process-wide embedding model for ``model_name``, loaded once.

    Deliberately global rather than per-store. ``all-MiniLM-L6-v2`` is ~90 MB
    resident and several seconds to load; with one tenant per persistence
    directory, a per-directory copy would mean a fresh 90 MB and a fresh cold
    start for every account that signs up. The model is stateless with respect
    to tenants - it turns text into vectors and knows nothing about whose text
    it is - so sharing it leaks nothing.
    """
    with _EMBEDDER_LOCK:
        embedder = _EMBEDDERS.get(model_name)
        if embedder is None:
            logger.info("loading embedding model %s on cpu", model_name)
            try:
                from transformers.utils import logging as hf_logging

                hf_logging.disable_progress_bar()
                hf_logging.set_verbosity_error()
            except ImportError:  # pragma: no cover - transformers ships with sentence-transformers
                pass
            embedder = KnowledgeStore._load_embedder(model_name)
            _EMBEDDERS[model_name] = embedder
        return embedder


class _SharedBackend:
    """One Chroma client and one worker thread per persistence directory.

    Each tenant has their own directory, so each gets their own client and its
    own serialised worker thread - Chroma's local client is synchronous and
    SQLite-backed, and one thread per directory is what stops two of that
    tenant's concurrent meetings colliding inside it. The embedding model is
    *not* per directory; see :func:`shared_embedder`.
    """

    _instances: dict[tuple[str, str], _SharedBackend] = {}
    _registry_lock = threading.RLock()

    def __init__(self, chroma_dir: Path, embedding_model: str) -> None:
        """Bind to a persistence directory and a model; nothing loads until :meth:`ensure`."""
        self.chroma_dir = chroma_dir
        self.embedding_model = embedding_model
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deleg8-knowledge")
        self.client: chromadb.ClientAPI | None = None
        self.embedder: SentenceTransformerEmbeddingFunction | None = None
        self._refs = 0

    @classmethod
    def acquire(cls, chroma_dir: Path, embedding_model: str) -> _SharedBackend:
        """The backend for this (directory, model), created on first request."""
        key = (str(chroma_dir), embedding_model)
        with cls._registry_lock:
            backend = cls._instances.get(key)
            if backend is None:
                backend = cls(chroma_dir, embedding_model)
                cls._instances[key] = backend
            backend._refs += 1
            return backend

    def release(self) -> None:
        """Drop one reference; the worker thread is kept for the life of the process.

        Stores are opened and closed as tenants come and go, and tearing the
        embedding model down on the last close would mean reloading 90 MB the
        next time anyone asks a question. The thread is cheap; the model is not.
        """
        with self._registry_lock:
            self._refs = max(0, self._refs - 1)

    def ensure(self) -> None:
        """Attach the shared embedder and open the client. Idempotent and cheap after the first call."""
        with self.lock:
            if self.client is not None and self.embedder is not None:
                return
            self.chroma_dir.mkdir(parents=True, exist_ok=True)
            self.embedder = shared_embedder(self.embedding_model)
            self.client = chromadb.PersistentClient(
                path=str(self.chroma_dir), settings=ChromaSettings(anonymized_telemetry=False)
            )

    def collection_names(self) -> list[str]:
        """Every collection in this directory (diagnostics and tenant cleanup)."""
        self.ensure()
        assert self.client is not None
        with self.lock:
            return [c.name for c in self.client.list_collections()]

    def drop_collection(self, name: str) -> bool:
        """Delete a whole collection - how a tenant's index is erased on account deletion."""
        self.ensure()
        assert self.client is not None
        with self.lock:
            try:
                self.client.delete_collection(name)
                return True
            except Exception as exc:  # noqa: BLE001 - "not found" is the common, harmless case
                logger.debug("could not delete collection %s: %s", name, exc)
                return False


class KnowledgeStore:
    """One Chroma collection plus the shared CPU embedder, behind the shared worker thread."""

    def __init__(self, settings: KnowledgeSettings) -> None:
        """Remember the settings; nothing is loaded until :meth:`open` (or first use)."""
        self._settings = settings
        self._backend = _SharedBackend.acquire(settings.chroma_dir, settings.embedding_model)
        self._collection: Collection | None = None
        self._closed = False

    @property
    def settings(self) -> KnowledgeSettings:
        """The settings this store was built with."""
        return self._settings

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Load the shared embedding model and open (or create) this collection. Idempotent."""
        self._backend.ensure()
        with self._lock:
            if self._collection is not None:
                return
            settings = self._settings
            assert self._backend.client is not None
            self._collection = self._backend.client.get_or_create_collection(
                name=settings.collection_name,
                embedding_function=self._backend.embedder,
                configuration={"hnsw": {"space": "cosine"}},
            )
            logger.info(
                "knowledge store ready: %s (%d chunks) at %s",
                settings.collection_name, self._collection.count(), settings.chroma_dir,
            )

    @staticmethod
    def _load_embedder(model_name: str) -> SentenceTransformerEmbeddingFunction:
        """Load from the local Hugging Face cache first; only contact the Hub when the model is absent."""
        try:
            return SentenceTransformerEmbeddingFunction(
                model_name=model_name, device="cpu", normalize_embeddings=True, local_files_only=True
            )
        except Exception as exc:  # noqa: BLE001 - any cache miss means "download it"
            logger.info("embedding model %s not cached locally (%s); downloading from Hugging Face", model_name, exc)
            return SentenceTransformerEmbeddingFunction(model_name=model_name, device="cpu", normalize_embeddings=True)

    async def open_async(self) -> None:
        """:meth:`open` on the worker thread so startup does not block the loop."""
        await self.run(self.open)

    def close(self) -> None:
        """Release this store's hold on the shared backend. The on-disk index stays intact.

        The shared worker thread and embedding model deliberately survive: they
        belong to the process, not to one tenant's store.
        """
        if self._closed:
            return
        self._closed = True
        self._collection = None
        self._backend.release()

    def drop(self) -> bool:
        """Delete this store's whole collection. Used when a tenant's data is erased."""
        name = self._settings.collection_name
        self._collection = None
        return self._backend.drop_collection(name)

    @property
    def is_open(self) -> bool:
        """True once :meth:`open` has completed."""
        return self._collection is not None

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    @property
    def collection(self) -> Collection:
        """The Chroma collection; opens the store on first access."""
        if self._collection is None:
            self.open()
        assert self._collection is not None
        return self._collection

    @property
    def _lock(self) -> threading.RLock:
        """The shared backend's lock; every collection in a directory shares it."""
        return self._backend.lock

    @property
    def lock(self) -> threading.RLock:
        """Guard for callers that issue several collection calls as one unit."""
        return self._backend.lock

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts`` with the shared model (unit-normalised vectors)."""
        if not texts:
            return []
        self._backend.ensure()
        embedder = self._backend.embedder
        assert embedder is not None
        with self._lock:
            vectors = embedder(list(texts))
        return [[float(x) for x in vector] for vector in vectors]

    def count(self) -> int:
        """Number of chunks in the index."""
        with self._lock:
            return self.collection.count()

    def reset(self) -> None:
        """Drop every chunk (keeps the collection and its configuration)."""
        with self._lock:
            collection = self.collection
            ids = collection.get(include=[])["ids"]
            if ids:
                collection.delete(ids=ids)
            logger.info("knowledge store reset: removed %d chunks", len(ids))

    async def run(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` on the shared worker thread and await its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._backend.executor, fn)

    def describe(self) -> str:
        """Status line for health checks."""
        if not self.is_open:
            return f"documents: not loaded ({self._settings.embedding_model})"
        return (
            f"documents: {self.count()} chunks, model {self._settings.embedding_model}, "
            f"threshold {self._settings.min_similarity:.2f}"
        )
