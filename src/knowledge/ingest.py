"""
Document ingestion: local files -> chunks -> embeddings -> ChromaDB.

Supported inputs (scanned recursively under ``DELEG8_DOCUMENTS_DIR``):

* ``.md`` / ``.markdown`` - split on headings first so every chunk carries its
  section path ("Project Alpha > Architecture"), then by size.
* ``.txt``                - split by size.
* ``.pdf``                - text extracted per page (pypdf); chunks carry the page.

Each chunk is stored with a deterministic id (``<relative path>::<n>``) and
the file's SHA-256, so re-running the ingestor is idempotent: unchanged files
are skipped, changed files have their old chunks removed before the new ones
are added, and ``--force`` re-embeds everything.

Command line::

    python -m src.knowledge.ingest                 # ingest DELEG8_DOCUMENTS_DIR
    python -m src.knowledge.ingest --documents docs/ --force
    python -m src.knowledge.ingest --reset         # wipe the index first
    python -m src.knowledge.ingest --stats         # print index size and exit
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from pydantic import BaseModel, Field
from pypdf import PdfReader

from src.knowledge.config import KnowledgeConfigError, KnowledgeSettings
from src.knowledge.store import KnowledgeStore

logger = logging.getLogger("deleg8.knowledge.ingest")

SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".pdf"})

_MARKDOWN_HEADERS: list[tuple[str, str]] = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_SECTION_SEPARATOR: str = " > "


class IngestError(RuntimeError):
    """Raised when a document cannot be read or parsed."""


@dataclass(frozen=True)
class DocumentPage:
    """One unit of source text: the whole file, or one PDF page."""

    text: str
    page: int | None


@dataclass(frozen=True)
class LoadedDocument:
    """A file read from disk, before chunking."""

    path: Path
    relpath: str
    """Path relative to the documents folder; used as the chunk ``source``."""
    title: str
    sha256: str
    pages: tuple[DocumentPage, ...]

    @property
    def characters(self) -> int:
        """Total characters across pages."""
        return sum(len(page.text) for page in self.pages)


@dataclass(frozen=True)
class Chunk:
    """One embeddable passage with its metadata."""

    chunk_id: str
    text: str
    metadata: dict[str, str | int | float | bool]


class FileIngestResult(BaseModel):
    """Outcome for one file."""

    source: str = Field(..., description="Path relative to the documents folder.")
    status: str = Field(..., description="'ingested', 'skipped' (unchanged), or 'failed'.")
    chunks: int = Field(default=0, ge=0, description="Chunks now stored for this file.")
    characters: int = Field(default=0, ge=0, description="Characters of extracted text.")
    detail: str | None = Field(default=None, description="Error message when failed.")


class IngestReport(BaseModel):
    """Outcome for one ingestion run."""

    documents_dir: str
    files: list[FileIngestResult] = Field(default_factory=list)
    index_size: int = Field(..., ge=0, description="Chunks in the collection after the run.")
    elapsed_seconds: float = Field(..., ge=0)

    @property
    def ingested(self) -> int:
        """Files whose chunks were (re)written."""
        return sum(1 for f in self.files if f.status == "ingested")

    @property
    def skipped(self) -> int:
        """Files left untouched because they had not changed."""
        return sum(1 for f in self.files if f.status == "skipped")

    @property
    def failed(self) -> int:
        """Files that could not be read."""
        return sum(1 for f in self.files if f.status == "failed")

    @property
    def chunks_written(self) -> int:
        """Chunks added during this run."""
        return sum(f.chunks for f in self.files if f.status == "ingested")

    def summary(self) -> str:
        """One-line human summary."""
        return (
            f"{self.ingested} ingested, {self.skipped} unchanged, {self.failed} failed; "
            f"{self.chunks_written} chunks written, {self.index_size} in index, {self.elapsed_seconds:.1f}s"
        )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def _title_from_markdown(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def _title_from_path(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip().title() or path.name


def load_document(path: Path, root: Path) -> LoadedDocument:
    """Read one supported file into a :class:`LoadedDocument`.

    Raises:
        IngestError: for unsupported suffixes, unreadable files, or empty text.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise IngestError(f"unsupported file type {suffix!r}")
    try:
        relpath = path.relative_to(root).as_posix()
    except ValueError:
        relpath = path.name
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    if suffix == ".pdf":
        try:
            reader = PdfReader(path)
            pages = tuple(
                DocumentPage(text=(page.extract_text() or "").strip(), page=number)
                for number, page in enumerate(reader.pages, start=1)
            )
        except Exception as exc:  # noqa: BLE001 - pypdf raises many concrete types
            raise IngestError(f"could not parse PDF: {exc}") from exc
        pages = tuple(p for p in pages if p.text)
        title = (reader.metadata.title if reader.metadata and reader.metadata.title else None) or _title_from_path(path)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        text = text.strip()
        pages = (DocumentPage(text=text, page=None),) if text else ()
        title = _title_from_markdown(text, _title_from_path(path)) if suffix in {".md", ".markdown"} else _title_from_path(path)

    if not pages:
        raise IngestError("no extractable text")
    return LoadedDocument(path=path, relpath=relpath, title=title, sha256=sha256, pages=pages)


def discover_documents(directory: Path) -> list[Path]:
    """Every supported file under ``directory``, sorted for reproducible ids."""
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith(".")
    )


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def _section_path(title: str, headers: dict[str, Any]) -> str:
    parts = [title]
    for key in ("h1", "h2", "h3"):
        value = str(headers.get(key, "")).strip()
        if value and value != title:
            parts.append(value)
    return _SECTION_SEPARATOR.join(parts)


def chunk_document(doc: LoadedDocument, settings: KnowledgeSettings) -> list[Chunk]:
    """Split a document into embeddable chunks that each carry their provenance.

    Every chunk's text starts with its section path so the embedding knows
    which document and section it belongs to; the retriever strips that line
    when it composes a spoken answer.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    is_markdown = doc.path.suffix.lower() in {".md", ".markdown"}
    header_splitter = MarkdownHeaderTextSplitter(_MARKDOWN_HEADERS, strip_headers=False)

    chunks: list[Chunk] = []
    for page in doc.pages:
        if is_markdown:
            sections = header_splitter.split_text(page.text)
            pieces = [(_section_path(doc.title, s.metadata), body) for s in sections for body in splitter.split_text(s.page_content)]
        else:
            pieces = [(doc.title, body) for body in splitter.split_text(page.text)]
        for section, body in pieces:
            body = body.strip()
            if not body:
                continue
            index = len(chunks)
            metadata: dict[str, str | int | float | bool] = {
                "source": doc.relpath,
                "title": doc.title,
                "section": section,
                "chunk_index": index,
                "file_sha256": doc.sha256,
            }
            if page.page is not None:
                metadata["page"] = page.page
            chunks.append(Chunk(chunk_id=f"{doc.relpath}::{index:04d}", text=f"{section}\n{body}", metadata=metadata))
    return chunks


# --------------------------------------------------------------------------- #
# Ingestor
# --------------------------------------------------------------------------- #

class DocumentIngestor:
    """Loads, chunks, embeds and stores documents. Safe to run repeatedly."""

    def __init__(self, store: KnowledgeStore, settings: KnowledgeSettings | None = None) -> None:
        """Bind to the store; ``settings`` defaults to the store's own."""
        self._store = store
        self._settings = settings or store.settings

    # -- synchronous core (runs on the store's worker thread when awaited) --

    def _stored_hash(self, relpath: str) -> str | None:
        result = self._store.collection.get(where={"source": relpath}, limit=1, include=["metadatas"])
        metadatas = result.get("metadatas") or []
        if not metadatas or not metadatas[0]:
            return None
        value = metadatas[0].get("file_sha256")
        return str(value) if value is not None else None

    def ingest_file_sync(self, path: Path, *, force: bool = False, root: Path | None = None) -> FileIngestResult:
        """Ingest one file. Unchanged files are skipped unless ``force``."""
        root = root or self._settings.documents_dir
        try:
            doc = load_document(path, root)
        except (IngestError, OSError) as exc:
            logger.warning("skipping %s: %s", path, exc)
            return FileIngestResult(source=path.name, status="failed", detail=str(exc))

        with self._store.lock:
            collection = self._store.collection
            if not force and self._stored_hash(doc.relpath) == doc.sha256:
                existing = len(collection.get(where={"source": doc.relpath}, include=[])["ids"])
                return FileIngestResult(source=doc.relpath, status="skipped", chunks=existing, characters=doc.characters)

            chunks = chunk_document(doc, self._settings)
            collection.delete(where={"source": doc.relpath})
            if chunks:
                collection.add(
                    ids=[c.chunk_id for c in chunks],
                    documents=[c.text for c in chunks],
                    metadatas=[c.metadata for c in chunks],
                )
        logger.info("ingested %s: %d chunks from %d chars", doc.relpath, len(chunks), doc.characters)
        return FileIngestResult(source=doc.relpath, status="ingested", chunks=len(chunks), characters=doc.characters)

    def ingest_directory_sync(self, directory: Path | None = None, *, force: bool = False) -> IngestReport:
        """Ingest every supported file under ``directory`` (default: the configured folder)."""
        directory = directory or self._settings.documents_dir
        started = time.monotonic()
        self._store.open()
        files = [self.ingest_file_sync(path, force=force, root=directory) for path in discover_documents(directory)]
        return IngestReport(
            documents_dir=str(directory),
            files=files,
            index_size=self._store.count(),
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    # -- async facade ---------------------------------------------------

    async def ingest_file(self, path: Path, *, force: bool = False, root: Path | None = None) -> FileIngestResult:
        """Await :meth:`ingest_file_sync` on the store's worker thread."""
        return await self._store.run(lambda: self.ingest_file_sync(path, force=force, root=root))

    async def ingest_directory(self, directory: Path | None = None, *, force: bool = False) -> IngestReport:
        """Await :meth:`ingest_directory_sync` on the store's worker thread."""
        return await self._store.run(lambda: self.ingest_directory_sync(directory, force=force))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--documents", type=Path, default=None, help="folder to ingest (default: DELEG8_DOCUMENTS_DIR)")
    parser.add_argument("--force", action="store_true", help="re-embed files even when unchanged")
    parser.add_argument("--reset", action="store_true", help="remove every chunk from the index before ingesting")
    parser.add_argument("--stats", action="store_true", help="print the index size and exit")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    try:
        settings = KnowledgeSettings.from_env()
    except KnowledgeConfigError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    store = KnowledgeStore(settings)
    try:
        await store.open_async()
        if args.stats:
            print(store.describe())
            return 0
        if args.reset:
            await store.run(store.reset)
            print("Index reset.")
        directory = (args.documents or settings.documents_dir).resolve()
        print(f"Ingesting {directory} into {settings.chroma_dir} ({settings.embedding_model}, cpu)")
        report = await DocumentIngestor(store, settings).ingest_directory(directory, force=args.force)
        if not report.files:
            print(f"  no .md/.txt/.pdf files found under {directory}")
        for item in report.files:
            detail = f" - {item.detail}" if item.detail else ""
            print(f"  [{item.status:8}] {item.source}: {item.chunks} chunks, {item.characters} chars{detail}")
        print(report.summary())
        return 1 if report.failed and not report.ingested and not report.skipped else 0
    finally:
        store.close()


def main(argv: list[str]) -> int:
    """Entry point for ``python -m src.knowledge.ingest``."""
    from dotenv import load_dotenv

    load_dotenv()
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
