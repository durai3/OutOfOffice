"""
The principal's dossier: one canonical Markdown file the delegate always knows.

Chunk-and-retrieve is the right tool for a large, open-ended corpus. It is the
wrong tool for "who is this person": the facts that come up constantly in a
meeting - name, university, GPA, current role, what they applied for, when
they are free - are a couple of thousand tokens in total, they are needed on
almost every turn, and a retrieval miss on any of them makes the delegate look
like it does not know its own principal.

Worse, retrieval was never really supplying context here anyway: the answer
path found the relevant *chunks*, then threw them away and reloaded the whole
source documents (see :func:`~src.knowledge.retriever._load_full_document_context`).
Retrieval's actual job had shrunk to routing, and it was failing at that job
against garbled PDF text and four-word follow-up questions.

So the dossier inverts it. Once, at build time, an LLM reads every ingested
document and compiles them into one clean Markdown file under fixed headings.
That file is small enough to sit in the system prompt of every single turn, so
personal facts need no retrieval at all and cannot be missed. The document
index stays for the long tail - project docs, specifications, anything too
large or too numerous to inline - and for citations.

The dossier is also the product's trust surface: it is exactly "what my
delegate will say about me", in plain Markdown, editable by the principal. A
hand-edited dossier is never overwritten by a rebuild unless the caller forces
it, because the principal's correction always outranks the compiler.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from src.health import COMPONENT_DOSSIER, report_degraded, report_healthy
from src.knowledge.config import PROJECT_ROOT, KnowledgeSettings
from src.knowledge.ingest import IngestError, discover_documents, load_document
from src.llm import LlmClient, LlmError, ModelRole, build_llm

logger = logging.getLogger("deleg8.knowledge.dossier")

DOSSIER_DIR: Path = PROJECT_ROOT / "data" / "dossier"

SECTIONS: tuple[str, ...] = (
    "Identity",
    "Education",
    "Experience",
    "Skills",
    "Projects",
    "Applications",
    "Availability",
    "Contact",
)
"""Fixed heading set. A stable skeleton means the principal always knows where
to look to correct something, and the planner's prompt can refer to sections by
name without the compiler inventing its own taxonomy each rebuild."""

_MAX_SOURCE_CHARS: int = 400_000
"""Cap on the total document text handed to one compile call."""

_BUILD_SYSTEM_PROMPT: str = """\
You are compiling a factual dossier about {principal} from their own documents \
(CVs, transcripts, cover letters, project notes). This dossier is the single \
source of truth an AI delegate will use to answer questions on {principal}'s \
behalf in live meetings, so it must be accurate, specific, and complete.

Rules:
- Use ONLY what the supplied documents state. Never infer, estimate, or fill a \
  gap with something plausible. A missing fact is fine; a wrong one is not.
- Write every fact as a short, self-contained bullet that makes sense read \
  aloud on its own. "Cumulative GPA: 4.50 out of 5.00 (NTU)" - not "GPA: 4.50".
- Prefer specifics over summaries. Exact institution names, exact titles, exact \
  dates, exact figures, exact technologies. These are what people actually ask.
- Where documents disagree, state both and say which document each came from.
- If a section has nothing in the documents, write exactly: Not recorded.
- Do not include anything that reads as sensitive unless it is plainly \
  professional context: no government identifiers, no full home address, no \
  passwords, no bank details. Contact details limited to email and city.

Fill one field per section:
{sections}

Also produce `headline`: one sentence introducing {principal} professionally, \
as the delegate would use it if asked "tell me about yourself"."""


class CompiledDossier(BaseModel):
    """What the compiler asks the model to return.

    Deliberately **flat**: one ``list[str]`` per section rather than a
    ``list[Section]`` of objects. ``gemini-3.6-flash`` rejects a response schema
    containing an array of objects and reports it as a 503 "experiencing high
    demand", which is indistinguishable from a real outage and sent the retry
    logic chasing a capacity problem that did not exist. Every structured schema
    in this codebase stays flat for that reason.
    """

    headline: str = Field(..., description="One-sentence professional introduction.")
    identity: list[str] = Field(default_factory=list, description="Name, nationality, current status.")
    education: list[str] = Field(default_factory=list, description="Institutions, degrees, dates, GPA.")
    experience: list[str] = Field(default_factory=list, description="Roles, employers, dates, what was done.")
    skills: list[str] = Field(default_factory=list, description="Languages, frameworks, tools, competencies.")
    projects: list[str] = Field(default_factory=list, description="Named projects and what they are.")
    applications: list[str] = Field(default_factory=list, description="Companies applied to, roles, status.")
    availability: list[str] = Field(default_factory=list, description="Notice period, start dates, location.")
    contact: list[str] = Field(default_factory=list, description="Email and city only.")

    def bullets_for(self, heading: str) -> list[str]:
        """The compiled bullets for one of :data:`SECTIONS`."""
        return list(getattr(self, heading.strip().lower(), []) or [])


class DossierMeta(BaseModel):
    """Sidecar record of how a dossier file came to exist."""

    user_id: str
    built_at: datetime
    built_by: str = Field(..., description="Model that compiled it, or 'hand-edited'.")
    sources: list[str] = Field(default_factory=list, description="Document paths it was compiled from.")
    source_fingerprint: str = Field(default="", description="Hash of the source set; a rebuild is a no-op when unchanged.")
    hand_edited: bool = Field(default=False, description="True once a human has saved over the compiled text.")


@dataclass(frozen=True)
class Dossier:
    """A dossier as loaded from disk."""

    user_id: str
    markdown: str
    meta: DossierMeta | None

    @property
    def is_empty(self) -> bool:
        """True when there is no usable content."""
        return not self.markdown.strip()

    def describe(self) -> str:
        """Status line."""
        if self.is_empty:
            return "dossier: none (run python -m src.knowledge.dossier)"
        words = len(self.markdown.split())
        if self.meta is None:
            return f"dossier: {words} words (no build record)"
        origin = "hand-edited" if self.meta.hand_edited else self.meta.built_by
        return f"dossier: {words} words, {origin}, built {self.meta.built_at:%Y-%m-%d %H:%M}"


def render_markdown(compiled: CompiledDossier, *, principal: str) -> str:
    """Render a compiled dossier as the Markdown file that is stored and edited."""
    lines = [f"# {principal}", "", compiled.headline.strip(), ""]
    for heading in SECTIONS:
        lines.append(f"## {heading}")
        bullets = [b.strip() for b in compiled.bullets_for(heading) if b.strip()]
        lines.extend(f"- {b}" for b in bullets) if bullets else lines.append("Not recorded.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class DossierStore:
    """Reads and writes ``data/dossier/<user_id>.md`` plus its ``.meta.json`` sidecar."""

    def __init__(self, directory: Path | None = None) -> None:
        """``directory`` defaults to ``data/dossier``."""
        self._dir = directory or DOSSIER_DIR

    @property
    def directory(self) -> Path:
        """Where dossiers are kept."""
        return self._dir

    def path_for(self, user_id: str) -> Path:
        """Markdown path for ``user_id``."""
        return self._dir / f"{user_id}.md"

    def _meta_path(self, user_id: str) -> Path:
        return self._dir / f"{user_id}.meta.json"

    def load(self, user_id: str) -> Dossier:
        """Load the dossier, returning an empty one when none has been built."""
        path = self.path_for(user_id)
        if not path.exists():
            return Dossier(user_id=user_id, markdown="", meta=None)
        markdown = path.read_text(encoding="utf-8")
        meta: DossierMeta | None = None
        meta_path = self._meta_path(user_id)
        if meta_path.exists():
            try:
                meta = DossierMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt sidecar must not hide the dossier itself
                logger.warning("could not read dossier metadata for %s; treating as unknown origin", user_id)
        return Dossier(user_id=user_id, markdown=markdown, meta=meta)

    def save(self, user_id: str, markdown: str, meta: DossierMeta) -> Path:
        """Write the dossier and its sidecar; returns the Markdown path."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(user_id)
        path.write_text(markdown, encoding="utf-8")
        self._meta_path(user_id).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        logger.info("dossier saved for %s: %d chars (%s)", user_id, len(markdown), meta.built_by)
        return path

    def save_edit(self, user_id: str, markdown: str) -> Path:
        """Save a human edit, preserving build provenance but marking it hand-edited."""
        existing = self.load(user_id)
        meta = existing.meta or DossierMeta(
            user_id=user_id, built_at=datetime.now(timezone.utc), built_by="hand-edited",
        )
        meta = meta.model_copy(update={"hand_edited": True, "built_at": datetime.now(timezone.utc)})
        return self.save(user_id, markdown, meta)


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #

def _fingerprint(sources: dict[str, str]) -> str:
    """Stable hash of the source set, so an unchanged corpus skips a rebuild."""
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(name.encode("utf-8"))
        digest.update(sources[name].encode("utf-8"))
    return digest.hexdigest()[:16]


def collect_source_text(settings: KnowledgeSettings) -> dict[str, str]:
    """Full text of every ingestable document, keyed by relative path."""
    collected: dict[str, str] = {}
    for path in discover_documents(settings.documents_dir):
        try:
            doc = load_document(path, settings.documents_dir)
        except (IngestError, OSError) as exc:
            logger.warning("dossier: skipping %s (%s)", path.name, exc)
            continue
        collected[doc.relpath] = "\n\n".join(page.text for page in doc.pages)
    return collected


class DossierBuilder:
    """Compiles the document corpus into one dossier."""

    def __init__(self, llm: LlmClient | None = None, store: DossierStore | None = None) -> None:
        """``llm`` defaults to the configured provider; ``store`` to ``data/dossier``."""
        self._llm = llm if llm is not None else build_llm()
        self._store = store or DossierStore()

    @property
    def store(self) -> DossierStore:
        """Where compiled dossiers are written."""
        return self._store

    async def build(
        self,
        *,
        user_id: str,
        principal: str,
        settings: KnowledgeSettings,
        force: bool = False,
    ) -> Dossier:
        """Compile and save the dossier for ``user_id``.

        A hand-edited dossier is returned untouched unless ``force``; an
        unchanged corpus is likewise a no-op, so this is cheap to call on every
        document upload.

        Raises:
            LlmError: when no provider is configured or the compile call fails.
        """
        existing = self._store.load(user_id)
        sources = collect_source_text(settings)
        if not sources:
            raise LlmError(f"no documents under {settings.documents_dir} to compile a dossier from")
        fingerprint = _fingerprint(sources)

        if existing.meta is not None and not force:
            if existing.meta.hand_edited:
                logger.info("dossier for %s is hand-edited; not rebuilding (pass force=True to override)", user_id)
                return existing
            if existing.meta.source_fingerprint == fingerprint and not existing.is_empty:
                logger.info("dossier for %s is current (%d documents unchanged)", user_id, len(sources))
                return existing

        if self._llm is None:
            raise LlmError("no language model configured; cannot compile a dossier")

        budget = _MAX_SOURCE_CHARS
        parts: list[str] = []
        for name in sorted(sources):
            text = sources[name][:budget]
            if not text:
                continue
            parts.append(f"=== {name} ===\n{text}")
            budget -= len(text)
            if budget <= 0:
                break

        system = _BUILD_SYSTEM_PROMPT.format(
            principal=principal, sections="\n".join(f"- {s.lower()}: {s}" for s in SECTIONS)
        )
        compiled = await self._llm.generate(
            system=system,
            user="Documents:\n\n" + "\n\n".join(parts),
            schema=CompiledDossier,
            role=ModelRole.SMART,
            max_output_tokens=8192,
        )
        markdown = render_markdown(compiled, principal=principal)
        meta = DossierMeta(
            user_id=user_id,
            built_at=datetime.now(timezone.utc),
            built_by=self._llm.model_for(ModelRole.SMART),
            sources=sorted(sources),
            source_fingerprint=fingerprint,
            hand_edited=False,
        )
        self._store.save(user_id, markdown, meta)
        report_healthy(COMPONENT_DOSSIER)
        return Dossier(user_id=user_id, markdown=markdown, meta=meta)

    async def build_quietly(self, *, user_id: str, principal: str, settings: KnowledgeSettings, force: bool = False) -> Dossier:
        """:meth:`build`, but a failure is recorded as a degradation instead of raising.

        Catches everything, not just :class:`LlmError`. This runs on the way
        into a meeting, and the caller's only sensible response to *any*
        failure here is the same one: join with whatever dossier exists and say
        out loud that it is stale. A read-only store raising on ``save`` used to
        escape this and abort the join outright.
        """
        try:
            return await self.build(user_id=user_id, principal=principal, settings=settings, force=force)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            report_degraded(
                COMPONENT_DOSSIER,
                "could not compile the dossier; personal questions fall back to document search",
                str(exc),
            )
            return self._store.load(user_id)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile the principal's dossier from data/documents/.")
    parser.add_argument("--user", default=None, help="principal id (default: DELEG8_VOICE_USER_ID or 'durai')")
    parser.add_argument("--force", action="store_true", help="rebuild even if hand-edited or unchanged")
    parser.add_argument("--show", action="store_true", help="print the current dossier and exit")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    from src.brain.profile_store import ProfileNotFoundError, profile_store

    user_id = args.user or "durai"
    store = DossierStore()
    if args.show:
        dossier = store.load(user_id)
        print(dossier.describe())
        print()
        print(dossier.markdown or "(nothing built yet)")
        return 0

    try:
        principal = profile_store.get_profile(user_id).display_name
    except ProfileNotFoundError:
        principal = user_id

    settings = KnowledgeSettings.from_env()
    builder = DossierBuilder()
    print(f"Compiling dossier for {principal} from {settings.documents_dir} ...")
    try:
        dossier = await builder.build(user_id=user_id, principal=principal, settings=settings, force=args.force)
    except LlmError as exc:
        print(f"\nCould not build the dossier: {exc}")
        return 1
    print(dossier.describe())
    print(f"  -> {store.path_for(user_id)}")
    print()
    print(dossier.markdown)
    return 0


def main(argv: list[str]) -> int:
    """Entry point for ``python -m src.knowledge.dossier``."""
    from dotenv import load_dotenv

    load_dotenv()
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
