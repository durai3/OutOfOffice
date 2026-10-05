"""
The knowledge seam of the Policy Router Brain.

The engine decides *whether* the delegate may speak; a :class:`KnowledgeSource`
decides *what grounded answer exists*. The engine never improvises: when no
source returns a :class:`GroundedAnswer` the turn escalates or stays silent.

Two sources live here because they have no heavy dependencies:

* :class:`ProfileKnowledge`   - the curated facts on the Representation Policy
  (Sprint 1). Keyword matched, verbatim answers, tied to a policy topic.
* :class:`CompositeKnowledge` - tries several sources in order; the first
  grounded answer wins. Curated profile facts are consulted before documents
  because their wording was approved by the principal.

The document-backed source (ChromaDB + sentence-transformers) is
:class:`src.knowledge.retriever.DocumentKnowledge`; it implements the same
protocol so the brain package stays free of vector-store dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from src.brain.models import Grounding, GroundingSource, KnowledgeEntry, UserProfile
from src.brain.text import matched_phrases

DOCUMENTED_FACTS_TOPIC: str = "documented_facts"
"""Policy topic that authorises answers grounded in ingested documents.

When an utterance matches no lexical trigger but the document index holds a
confident answer, the engine classifies it under this topic. The profile must
carry a GREEN rule with this topic for that path to be active; without it the
delegate never volunteers document facts.
"""


@dataclass(frozen=True)
class GroundedAnswer:
    """An answer the delegate may state, with the evidence it rests on."""

    answer: str
    grounding: Grounding


@runtime_checkable
class KnowledgeSource(Protocol):
    """Something that can ground an answer for a query on a given policy topic."""

    def describe(self) -> str:
        """One-line status for health checks and logs."""
        ...

    def lookup(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Return a grounded answer or ``None``. Synchronous; may block briefly."""
        ...

    async def lookup_async(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Async variant that keeps CPU-bound work off the event loop."""
        ...


class ProfileKnowledge:
    """Curated facts from :attr:`UserProfile.knowledge_base`, matched by keyword."""

    def describe(self) -> str:
        """Status line."""
        return "profile knowledge base (curated facts)"

    def lookup(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Pick the entry for ``topic`` whose keywords best cover the normalised ``query``."""
        best: KnowledgeEntry | None = None
        best_score = 0
        best_hits: tuple[str, ...] = ()
        for entry in profile.knowledge_for(topic):
            hits = matched_phrases(query, entry.keywords)
            if len(hits) > best_score:
                best, best_score, best_hits = entry, len(hits), hits
        if best is None:
            return None
        return GroundedAnswer(
            answer=best.answer,
            grounding=Grounding(
                source=GroundingSource.PROFILE,
                subject=best.subject,
                similarity=None,
                citations=[f"profile:{best.topic}/{best.subject}"],
                detail=f"keywords matched: {', '.join(best_hits)}",
            ),
        )

    async def lookup_async(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Keyword matching is microseconds; no thread hop needed."""
        return self.lookup(query, topic, profile)


class CompositeKnowledge:
    """Consults sources in order and returns the first grounded answer."""

    def __init__(self, sources: Sequence[KnowledgeSource]) -> None:
        """``sources`` are tried front to back; put the most authoritative first."""
        if not sources:
            raise ValueError("CompositeKnowledge needs at least one source")
        self._sources: tuple[KnowledgeSource, ...] = tuple(sources)

    @property
    def sources(self) -> tuple[KnowledgeSource, ...]:
        """The wrapped sources, in consultation order."""
        return self._sources

    def describe(self) -> str:
        """Status line listing every source."""
        return " -> ".join(source.describe() for source in self._sources)

    def lookup(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """First grounded answer wins."""
        for source in self._sources:
            answer = source.lookup(query, topic, profile)
            if answer is not None:
                return answer
        return None

    async def lookup_async(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """First grounded answer wins; sources are awaited sequentially."""
        for source in self._sources:
            answer = await source.lookup_async(query, topic, profile)
            if answer is not None:
                return answer
        return None
