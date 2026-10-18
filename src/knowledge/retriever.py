"""
Retrieval over the document index, with a confidence gate.

:class:`KnowledgeRetriever` embeds a meeting question, fetches the nearest
chunks from ChromaDB, and keeps only those whose cosine similarity clears
``min_similarity``. When nothing clears it the result is flagged
:attr:`RetrievalStatus.UNKNOWN_CONTEXT`, which the brain treats exactly like
"no grounded answer": escalate or stay silent, never improvise.

:class:`AnswerComposer` turns a grounded result into something the delegate
can say. It is extractive: the candidate sentences of the grounded chunks are
re-scored against the question and the best one (plus its neighbour when it
also fits) is returned verbatim. The sentence score is a hybrid: cosine
similarity from the same CPU embedder, plus a bonus for the question's key
terms that the sentence actually contains. The bonus is what stops "What
database is Project Alpha using?" from being answered by the sentence that
merely says what Project Alpha *is*. It never states anything that is not in
the documents, but it also cannot synthesise across passages or vary its
phrasing per question - different questions that retrieve the same chunk can
get the same sentence back.

:class:`ClaudeAnswerSynthesiser` (Anthropic) and :class:`GeminiAnswerSynthesiser`
(Google) are the optional alternative (Sprint 7, Gemini added Sprint 8): when a
provider key is set, the synthesiser reads the *full text* of the source
document(s) the retrieval gate identified as relevant - not just the small
retrieved snippets - and writes one synthesised answer, constrained by its
prompt to use only what those documents say and to quote its exact supporting
sentence(s), which are then checked programmatically
(:func:`~src.knowledge.grounding.verify_citations`) before the answer is
trusted. :class:`DocumentKnowledge` prefers the synthesiser when available and
falls back to the extractive composer - on a missing key, a declined answer,
an unverified citation, or any API failure - so behaviour without a key is
unchanged. :func:`build_synthesiser` prefers Gemini, then Claude, then the
extractive-only fallback, so the same code runs whichever provider you have
credentials for.

:class:`DocumentKnowledge` adapts both to the brain's
:class:`~src.brain.knowledge.KnowledgeSource` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from enum import Enum
from typing import Protocol, runtime_checkable

import anthropic
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, computed_field

from src.brain.knowledge import (
    CompositeKnowledge,
    GroundedAnswer,
    KnowledgeSource,
    ProfileKnowledge,
)
from src.brain.models import Grounding, GroundingSource, UserProfile
from src.brain.text import split_sentences
from src.knowledge.config import KnowledgeSettings
from src.knowledge.grounding import verify_citations
from src.knowledge.ingest import IngestError, load_document
from src.knowledge.store import KnowledgeStore
from src.llm import choose_provider

logger = logging.getLogger("deleg8.knowledge.retriever")


def cosine_similarity(distance: float) -> float:
    """Chroma's cosine distance as a similarity, clamped to [-1.0, 1.0].

    The clamp is not defensive tidiness. Chroma's HNSW index returns a distance
    computed in float32, so for a near-perfect match it can come back very
    slightly negative - ``-0.0079`` was observed against a short document - and
    ``1 - distance`` is then ``1.0079``. :class:`~src.brain.models.Grounding`
    bounds similarity at 1.0, so that turn raised a validation error *inside a
    live meeting*, on the single best match the index had. A tiny arithmetic
    artefact must not be the difference between answering and crashing.
    """
    return round(max(-1.0, min(1.0, 1.0 - float(distance))), 4)


class RetrievalStatus(str, Enum):
    """Whether the index holds context relevant enough to answer from."""

    GROUNDED = "GROUNDED"
    """At least one passage cleared the similarity threshold."""
    UNKNOWN_CONTEXT = "UNKNOWN_CONTEXT"
    """Nothing relevant enough was found; the caller must not answer from memory."""


UNKNOWN_CONTEXT: RetrievalStatus = RetrievalStatus.UNKNOWN_CONTEXT
"""Convenience alias so callers can compare against ``retriever.UNKNOWN_CONTEXT``."""


class RetrievedChunk(BaseModel):
    """One passage returned by the index."""

    chunk_id: str
    text: str = Field(..., description="Chunk text as stored (first line is the section path).")
    source: str = Field(..., description="File path relative to the documents folder.")
    title: str
    section: str
    page: int | None = None
    similarity: float = Field(
        ..., description="Cosine similarity to the query (1 - distance), plus a bounded term-overlap bonus."
    )

    @property
    def body(self) -> str:
        """Chunk text without the leading section-path line."""
        first, _, rest = self.text.partition("\n")
        return rest if first.strip() == self.section.strip() else self.text

    @property
    def citation(self) -> str:
        """Human-readable pointer, e.g. ``project_alpha.md > Architecture (p. 3)``."""
        _, _, tail = self.section.partition(" > ")
        where = f"{self.source} > {tail}" if tail else self.source
        return f"{where} (p. {self.page})" if self.page is not None else where


class RetrievalResult(BaseModel):
    """What the index said about one query."""

    query: str
    status: RetrievalStatus
    chunks: list[RetrievedChunk] = Field(default_factory=list, description="Passages above the threshold, best first.")
    best_similarity: float | None = Field(default=None, description="Top score seen, even if below threshold.")
    threshold: float
    index_size: int = Field(..., ge=0)
    elapsed_ms: float = Field(..., ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grounded(self) -> bool:
        """True when the delegate may answer from ``chunks``."""
        return self.status is RetrievalStatus.GROUNDED

    def citations(self) -> list[str]:
        """Citations of the passages that cleared the threshold."""
        return [chunk.citation for chunk in self.chunks]


_CANDIDATE_POOL: int = 16
"""Passages fetched from the vector index before reranking down to ``top_k``."""
_LEXICAL_WEIGHT: float = 0.20
"""Largest bonus the term-overlap reranker can add to a cosine similarity."""


def _hybrid_similarity(chunk: RetrievedChunk, query_terms: set[str]) -> float:
    """Cosine similarity plus a bonus for the question's own words appearing in the passage.

    Dense retrieval answers "is this about the same thing", and is weak at "does
    this contain the specific word asked about". Adding a bounded lexical term
    (the share of the question's content words found in the passage, section
    heading included) is the standard cheap hybrid. The bonus needs at least one
    matching term, and scales with how much of the question is covered, so a
    passage that merely shares a stopword with the query gains nothing - and an
    unrelated question still falls below the gate.
    """
    if not query_terms:
        return chunk.similarity
    overlap = len(query_terms & _terms(f"{chunk.section} {chunk.text}")) / len(query_terms)
    return min(1.0, chunk.similarity + _LEXICAL_WEIGHT * overlap)


_CLAUSE_SPLIT = re.compile(r"\s*(?:[?;]|,|\band\b|\balso\b|\bplus\b|\bas well as\b)\s*", re.IGNORECASE)
_MAX_SUBQUERIES: int = 3
_MIN_CLAUSE_TERMS: int = 1


def split_compound_query(query: str) -> list[str]:
    """The sub-questions inside a compound one, or ``[]`` when it is a single question.

    "What database does Atlas run on, and what is the median load time?" is two
    questions, and one embedding of the whole sentence lands between them and
    retrieves neither well. Each clause is kept only if it carries a content word
    of its own ("and what" does not), and at most :data:`_MAX_SUBQUERIES` are used
    so a rambling utterance cannot multiply the retrieval cost.
    """
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(query) if part and part.strip()]
    clauses = [part for part in parts if len(_terms(part)) >= _MIN_CLAUSE_TERMS]
    return clauses[:_MAX_SUBQUERIES] if len(clauses) >= 2 else []


class KnowledgeRetriever:
    """Semantic search over the store with a similarity gate."""

    def __init__(self, store: KnowledgeStore, settings: KnowledgeSettings | None = None) -> None:
        """Bind to the store; ``settings`` defaults to the store's own."""
        self._store = store
        self._settings = settings or store.settings

    @property
    def store(self) -> KnowledgeStore:
        """The underlying store."""
        return self._store

    @property
    def threshold(self) -> float:
        """Similarity a passage must reach to be returned."""
        return self._settings.min_similarity

    def retrieve_sync(self, query: str, *, top_k: int | None = None, min_similarity: float | None = None) -> RetrievalResult:
        """Query the index synchronously (blocks for the embedding, ~10 ms on CPU)."""
        query = " ".join(query.split())
        k = top_k or self._settings.top_k
        threshold = self._settings.min_similarity if min_similarity is None else min_similarity
        started = time.monotonic()
        with self._store.lock:
            collection = self._store.collection
            size = collection.count()
            if not query or size == 0:
                return RetrievalResult(
                    query=query, status=RetrievalStatus.UNKNOWN_CONTEXT, threshold=threshold,
                    index_size=size, elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                )
            # Over-fetch, then rerank: a 90 MB embedding model blurs short factual
            # chunks ("Primary database: PostgreSQL 16") behind longer prose that
            # merely mentions the same product, and top-k alone then drops the one
            # chunk that holds the answer. The pool is cheap to widen (CPU, in
            # memory); the reranker below puts exact-term evidence back in front.
            variants = [query, *split_compound_query(query)]
            raws = [
                collection.query(
                    query_texts=[variant], n_results=min(max(k * 4, _CANDIDATE_POOL), size),
                    include=["documents", "metadatas", "distances"],
                )
                for variant in variants
            ]

        # Score every candidate against the question that surfaced it and keep its
        # best score, so a passage that answers one half of a compound question is
        # not outranked by passages that merely resemble the whole sentence.
        best: dict[str, RetrievedChunk] = {}
        for variant, raw in zip(variants, raws):
            variant_terms = _terms(variant)
            ids = raw["ids"][0]
            documents = (raw.get("documents") or [[]])[0]
            metadatas = (raw.get("metadatas") or [[]])[0]
            distances = (raw.get("distances") or [[]])[0]
            for chunk_id, text, meta, distance in zip(ids, documents, metadatas, distances):
                meta = meta or {}
                page = meta.get("page")
                chunk = RetrievedChunk(
                    chunk_id=chunk_id,
                    text=text or "",
                    source=str(meta.get("source", "unknown")),
                    title=str(meta.get("title", "")),
                    section=str(meta.get("section", "")),
                    page=int(page) if isinstance(page, (int, float)) else None,
                    similarity=cosine_similarity(distance),
                )
                chunk = chunk.model_copy(update={"similarity": _hybrid_similarity(chunk, variant_terms)})
                held = best.get(chunk_id)
                if held is None or chunk.similarity > held.similarity:
                    best[chunk_id] = chunk
        candidates = sorted(best.values(), key=lambda c: c.similarity, reverse=True)
        # A compound question earns one extra slot per extra clause: it is asking
        # for more than one thing, and top-k was sized for one.
        candidates = candidates[: k + max(0, len(variants) - 1)]
        best = candidates[0].similarity if candidates else None
        kept = [c for c in candidates if c.similarity >= threshold]
        status = RetrievalStatus.GROUNDED if kept else RetrievalStatus.UNKNOWN_CONTEXT
        elapsed = round((time.monotonic() - started) * 1000, 2)
        logger.info(
            "retrieval %s best=%.3f threshold=%.2f kept=%d/%d (%.0fms) query=%r",
            status.value, best if best is not None else float("nan"), threshold, len(kept), len(candidates), elapsed, query,
        )
        return RetrievalResult(
            query=query, status=status, chunks=kept, best_similarity=best, threshold=threshold,
            index_size=size, elapsed_ms=elapsed,
        )

    async def retrieve(self, query: str, *, top_k: int | None = None, min_similarity: float | None = None) -> RetrievalResult:
        """Await :meth:`retrieve_sync` on the store's worker thread."""
        return await self._store.run(lambda: self.retrieve_sync(query, top_k=top_k, min_similarity=min_similarity))

    async def index_size(self) -> int:
        """Number of chunks in the index."""
        return await self._store.run(self._store.count)


# --------------------------------------------------------------------------- #
# Answer composition (extractive)
# --------------------------------------------------------------------------- #

_LIST_PREFIX = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+|#+\s*|>\s*)")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}")
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_MIN_SENTENCE_WORDS: int = 4
_MAX_ANSWER_CHARS: int = 360
_NEIGHBOUR_TOLERANCE: float = 0.12
"""A following sentence is appended when it scores within this much of the best."""
_KEYWORD_WEIGHT: float = 0.25
"""Maximum bonus added to cosine similarity when every key term of the question is present."""

_QUERY_STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been being by can could did do does doing for from had has have
    how i if in into is it its of on or our ours shall she he they them their should that the
    then there these this those to us was we were what when where which who whom why will with
    would you your yours about tell me please give know explain mean""".split()
)


def _stem(word: str) -> str:
    """Crude suffix stripping so 'uses', 'using' and 'used' all meet 'use'."""
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _terms(text: str) -> set[str]:
    """Stemmed content words of ``text``."""
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _QUERY_STOPWORDS and len(w) > 1}


def _key_terms(query: str, title: str) -> set[str]:
    """Question terms that discriminate between sentences: content words minus the document title."""
    terms = _terms(query) - _terms(title)
    return terms or _terms(query)


class ComposedAnswer(BaseModel):
    """A spoken answer grounded in a retrieval result - lifted verbatim, or synthesised by an LLM."""

    text: str
    similarity: float = Field(
        ..., ge=-1.0, le=1.0,
        description="Cosine similarity of the best sentence to the question. A real similarity, bounded.",
    )
    score: float = Field(
        default=0.0,
        description="Hybrid ranking score (cosine + key-term bonus). Used to choose the sentence, not reported.",
    )
    chunk: RetrievedChunk
    citations: list[str] = Field(
        default_factory=list, description="Passages the answer actually draws on; falls back to the chunk's own."
    )
    composer: str = Field(default="extractive", description="Which composer produced this answer.")


def _candidate_sentences(chunk: RetrievedChunk) -> list[str]:
    """Clean, speakable sentences from a chunk body (bullets and headings stripped)."""
    candidates: list[str] = []
    for line in chunk.body.splitlines():
        stripped = line.strip()
        if not stripped or _TABLE_RULE.match(stripped):
            continue
        if "|" in stripped:
            stripped = ", ".join(cell.strip() for cell in stripped.strip("|").split("|") if cell.strip())
        stripped = _LIST_PREFIX.sub("", stripped).strip()
        stripped = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
        stripped = re.sub(r"`(.+?)`", r"\1", stripped)
        for sentence in split_sentences(stripped):
            if len(sentence.split()) >= _MIN_SENTENCE_WORDS:
                candidates.append(sentence)
    return candidates


def _finish(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


class AnswerComposer:
    """Picks the sentence(s) of a grounded result that best answer the question."""

    def __init__(self, store: KnowledgeStore, *, min_similarity: float) -> None:
        """Sentences must reach ``min_similarity`` too, or the composer declines."""
        self._store = store
        self._min_similarity = min_similarity

    def compose(self, query: str, result: RetrievalResult) -> ComposedAnswer | None:
        """Return the best-supported answer, or ``None`` when no sentence is confident enough."""
        if not result.grounded:
            return None
        candidates: list[tuple[RetrievedChunk, int, str]] = [
            (chunk, position, sentence)
            for chunk in result.chunks
            for position, sentence in enumerate(_candidate_sentences(chunk))
        ]
        if not candidates:
            return None
        vectors = self._store.embed([query, *(sentence for _, _, sentence in candidates)])
        query_vector, sentence_vectors = vectors[0], vectors[1:]
        # Two numbers per sentence, and the distinction matters. The hybrid score
        # (cosine plus a key-term bonus) is what *ranks* sentences; the plain
        # cosine is what gets reported as the grounding's similarity. Conflating
        # them meant a perfect match with good keyword overlap produced a
        # "similarity" above 1.0, which Grounding rejects - so the single best
        # answer the index had was the one that crashed the turn.
        scores: list[float] = []
        cosines: list[float] = []
        for (chunk, _, sentence), vector in zip(candidates, sentence_vectors):
            cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector))))
            keys = _key_terms(query, chunk.title)
            overlap = len(keys & _terms(sentence)) / len(keys) if keys else 0.0
            cosines.append(cosine)
            scores.append(cosine + _KEYWORD_WEIGHT * overlap)
        best_index = max(range(len(scores)), key=scores.__getitem__)
        best_chunk, best_position, best_sentence = candidates[best_index]
        best_score = scores[best_index]
        best_cosine = cosines[best_index]
        if best_score < self._min_similarity:
            logger.info("composer declined: best sentence %.3f < %.2f", best_score, self._min_similarity)
            return None

        answer = _finish(best_sentence)
        follow = best_index + 1
        if follow < len(candidates):
            chunk, position, sentence = candidates[follow]
            adjacent = chunk.chunk_id == best_chunk.chunk_id and position == best_position + 1
            if adjacent and scores[follow] >= best_score - _NEIGHBOUR_TOLERANCE and len(answer) + len(sentence) <= _MAX_ANSWER_CHARS:
                answer = f"{answer} {_finish(sentence)}"
        return ComposedAnswer(
            text=answer, similarity=round(best_cosine, 4), score=round(best_score, 4), chunk=best_chunk,
            citations=[best_chunk.citation], composer="extractive",
        )


# --------------------------------------------------------------------------- #
# Answer composition (LLM synthesis, optional)
# --------------------------------------------------------------------------- #

class SynthesisError(RuntimeError):
    """Raised by an :class:`AnswerSynthesiser` when it cannot produce an answer."""


DEFAULT_ANSWER_MODEL: str = "claude-sonnet-5"
DEFAULT_GEMINI_ANSWER_MODEL: str = "gemini-3.6-flash"
"""The previous default, ``gemini-2.5-flash``, was retired and returns 404. Every
synthesis call failed, fell back to the extractive composer, and the delegate
spent a live meeting reading unrelated sentences out of PDFs - the failure that
:func:`src.llm.preflight_or_raise` now catches at startup.

The live answer path is :mod:`src.brain.planner`, which goes through
:mod:`src.llm`. This class remains for the legacy ``KnowledgeSource`` route used
by the diagnostics endpoints."""
ANSWER_TIMEOUT_SECONDS: float = 8.0
_MAX_SYNTHESISED_ANSWER_CHARS: int = 420

_ANSWER_SYSTEM_PROMPT: str = """\
You are DELEG8, an AI meeting delegate answering a question on behalf of the \
principal you represent, using only the source document(s) supplied below - \
they were selected because a passage of theirs already cleared a relevance \
threshold against this question, but the documents may still not actually \
answer it, and the relevant part may be anywhere in them, not just the \
passage that first matched.

Rules:
- Answer using ONLY the supplied document text. Never state a fact, number, \
  or date that is not in it, even if you believe it to be true.
- You may combine information from more than one place in the document(s).
- Keep the answer short and speakable: 1-3 sentences, well under 400 characters.
- supporting_quotes must be exact, verbatim sentence(s) copied character-for- \
  character from the supplied text - the evidence for your answer, not a \
  paraphrase. An automated check will reject the answer if a quote does not \
  appear verbatim in the source, so copy exactly.
- If, having read the document(s), they do not answer the question, set \
  answered to false and leave answer and supporting_quotes empty - do not \
  guess or improvise."""


class _AnswerOutput(BaseModel):
    """Structured verdict asked of the LLM."""

    answered: bool = Field(..., description="True when the supplied document(s) answer the question.")
    answer: str = Field(default="", description="The spoken answer; empty when answered is false.")
    supporting_quotes: list[str] = Field(
        default_factory=list,
        description=(
            "Exact, verbatim sentence(s) copied character-for-character from the supplied "
            "document text that support the answer. Not shown to the user; used only to "
            "verify the answer is actually grounded in the source."
        ),
    )


@runtime_checkable
class AnswerSynthesiser(Protocol):
    """Something that can turn a full document context into one synthesised answer."""

    def describe(self) -> str:
        """One-line status for logs."""
        ...

    async def synthesise(self, query: str, context: str, result: RetrievalResult) -> ComposedAnswer | None:
        """Return a synthesised answer, or ``None`` when ``context`` does not answer ``query``
        or the model's cited quotes do not verify against it.

        ``context`` is the full text of the document(s) the retrieval gate identified as
        relevant (see :func:`_load_full_document_context`) - not just the small chunks in
        ``result``, which is passed through only for its citation metadata.

        Raises:
            SynthesisError: on any technical failure (auth, rate limit, network, refusal).
        """
        ...


def _load_full_document_context(result: RetrievalResult, settings: KnowledgeSettings) -> str:
    """Full text of the distinct source documents behind ``result.chunks``, capped and ordered.

    Retrieval's job shrinks to "which document(s) are relevant" rather than "which
    3.2KB fragment is the whole answer" - the synthesiser reads the complete document(s),
    ordered by their best-scoring chunk, up to :attr:`KnowledgeSettings.max_context_documents`
    and :attr:`KnowledgeSettings.max_document_context_chars`. Reuses the same loader the
    ingestor uses (:func:`~src.knowledge.ingest.load_document`), so this is exactly the
    text that was embedded - no separate copy to keep in sync.
    """
    best_by_source: dict[str, float] = {}
    for chunk in result.chunks:
        if chunk.similarity > best_by_source.get(chunk.source, -1.0):
            best_by_source[chunk.source] = chunk.similarity
    ordered_sources = sorted(best_by_source, key=lambda s: best_by_source[s], reverse=True)

    parts: list[str] = []
    budget = settings.max_document_context_chars
    for source in ordered_sources[: settings.max_context_documents]:
        if budget <= 0:
            break
        try:
            doc = load_document(settings.documents_dir / source, settings.documents_dir)
        except (IngestError, OSError) as exc:
            logger.warning("could not reload full document %r for answer synthesis: %s", source, exc)
            continue
        text = "\n\n".join(page.text for page in doc.pages)[:budget]
        if not text:
            continue
        parts.append(f"=== {doc.title} ({source}) ===\n{text}")
        budget -= len(text)
    return "\n\n".join(parts)


class ClaudeAnswerSynthesiser:
    """Asks Claude to answer strictly from the full source document(s). Never improvises."""

    def __init__(
        self,
        model: str = DEFAULT_ANSWER_MODEL,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        timeout: float = ANSWER_TIMEOUT_SECONDS,
    ) -> None:
        """``client`` can be injected for tests; otherwise credentials come from the environment."""
        self._model = model
        self._client = client or anthropic.AsyncAnthropic(timeout=timeout)

    def describe(self) -> str:
        """Label."""
        return f"claude ({self._model})"

    async def synthesise(self, query: str, context: str, result: RetrievalResult) -> ComposedAnswer | None:
        """One structured-output request over the full document context."""
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=1024,
                system=_ANSWER_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"Question: {query}\n\nSource document(s):\n{context}",
                    }
                ],
                output_format=_AnswerOutput,
            )
        except anthropic.AuthenticationError as exc:
            raise SynthesisError("Anthropic rejected ANTHROPIC_API_KEY") from exc
        except anthropic.RateLimitError as exc:
            raise SynthesisError("Anthropic rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise SynthesisError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise SynthesisError(f"could not reach Anthropic: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = response.stop_details.explanation if response.stop_details else None
            raise SynthesisError(f"model declined to answer ({detail or 'no explanation'})")
        if response.parsed_output is None:
            raise SynthesisError(f"no structured output returned (stop_reason={response.stop_reason})")

        parsed = response.parsed_output
        logger.info(
            "claude answer synthesis: answered=%s, %d input / %d output tokens (request %s)",
            parsed.answered, response.usage.input_tokens, response.usage.output_tokens, response._request_id,
        )
        if not parsed.answered or not parsed.answer.strip():
            return None
        if not verify_citations(parsed.supporting_quotes, context):
            logger.warning("claude answer synthesis declined: supporting_quotes did not verify against the source text")
            return None
        best = result.chunks[0]
        return ComposedAnswer(
            text=parsed.answer.strip()[:_MAX_SYNTHESISED_ANSWER_CHARS],
            similarity=best.similarity,
            chunk=best,
            citations=result.citations(),
            composer=f"claude ({self._model})",
        )


class GeminiAnswerSynthesiser:
    """Asks Gemini to answer strictly from the full source document(s). Never improvises."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_ANSWER_MODEL,
        client: genai.Client | None = None,
        *,
        timeout: float = ANSWER_TIMEOUT_SECONDS,
    ) -> None:
        """``client`` can be injected for tests; otherwise credentials come from the environment."""
        self._model = model
        self._client = client or genai.Client()
        self._timeout = timeout

    def describe(self) -> str:
        """Label."""
        return f"gemini ({self._model})"

    async def synthesise(self, query: str, context: str, result: RetrievalResult) -> ComposedAnswer | None:
        """One structured-output request over the full document context."""
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=f"Question: {query}\n\nSource document(s):\n{context}",
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_ANSWER_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=_AnswerOutput,
                    ),
                ),
                timeout=self._timeout,
            )
        except genai_errors.APIError as exc:
            raise SynthesisError(f"Gemini API error {exc.code}: {exc.message}") from exc
        except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise SynthesisError(f"could not reach Gemini: {exc}") from exc

        parsed = response.parsed
        if parsed is None:
            finish = response.candidates[0].finish_reason if response.candidates else None
            raise SynthesisError(f"no structured output returned (finish_reason={finish})")

        usage = response.usage_metadata
        logger.info(
            "gemini answer synthesis: answered=%s, %s input / %s output tokens",
            parsed.answered,
            usage.prompt_token_count if usage else "?",
            usage.candidates_token_count if usage else "?",
        )
        if not parsed.answered or not parsed.answer.strip():
            return None
        if not verify_citations(parsed.supporting_quotes, context):
            logger.warning("gemini answer synthesis declined: supporting_quotes did not verify against the source text")
            return None
        best = result.chunks[0]
        return ComposedAnswer(
            text=parsed.answer.strip()[:_MAX_SYNTHESISED_ANSWER_CHARS],
            similarity=best.similarity,
            chunk=best,
            citations=result.citations(),
            composer=f"gemini ({self._model})",
        )


def build_synthesiser(model: str | None = None) -> AnswerSynthesiser | None:
    """Gemini when ``GEMINI_API_KEY`` is set (the default); else Claude when ``ANTHROPIC_API_KEY``
    is set; else ``None`` (extractive composer only). See :func:`src.llm.choose_provider`."""
    provider = choose_provider(
        claude=bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        gemini=bool(os.getenv("GEMINI_API_KEY", "").strip()),
    )
    if provider == "gemini":
        chosen = model or os.getenv("DELEG8_GEMINI_ANSWER_MODEL", "").strip() or DEFAULT_GEMINI_ANSWER_MODEL
        logger.info("answer synthesiser: gemini (%s)", chosen)
        return GeminiAnswerSynthesiser(chosen)
    if provider == "claude":
        chosen = model or os.getenv("DELEG8_ANSWER_MODEL", "").strip() or DEFAULT_ANSWER_MODEL
        logger.info("answer synthesiser: claude (%s)", chosen)
        return ClaudeAnswerSynthesiser(chosen)
    logger.info("answer synthesiser: none (no GEMINI_API_KEY or ANTHROPIC_API_KEY); extractive composer only")
    return None


# --------------------------------------------------------------------------- #
# Brain adapter
# --------------------------------------------------------------------------- #

class DocumentKnowledge:
    """:class:`KnowledgeSource` backed by the document index."""

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        composer: AnswerComposer | None = None,
        synthesiser: AnswerSynthesiser | None = None,
    ) -> None:
        """Bind the retriever; the composer defaults to one using the retriever's threshold.

        ``synthesiser`` (Sprint 7), when given, is consulted by :meth:`lookup_async`
        alone; :meth:`lookup` (the synchronous path, used only by scripts and
        worker threads) always stays extractive.
        """
        self._retriever = retriever
        self._composer = composer or AnswerComposer(retriever.store, min_similarity=retriever.threshold)
        self._synthesiser = synthesiser
        self.last_result: RetrievalResult | None = None
        """Most recent retrieval, kept for diagnostics and the verification script."""

    @property
    def retriever(self) -> KnowledgeRetriever:
        """The wrapped retriever."""
        return self._retriever

    def describe(self) -> str:
        """Status line."""
        composer = f", answers: {self._synthesiser.describe()}" if self._synthesiser is not None else ""
        return self._retriever.store.describe() + composer

    def lookup(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Retrieve, gate, and compose extractively. ``topic`` is informational: documents are not topic-scoped."""
        result = self._retriever.retrieve_sync(query)
        self.last_result = result
        if not result.grounded:
            return None
        composed = self._composer.compose(query, result)
        if composed is None:
            return None
        return self._to_grounded_answer(composed, topic, result)

    async def lookup_async(self, query: str, topic: str, profile: UserProfile) -> GroundedAnswer | None:
        """Retrieve (CPU-bound, on the store's worker thread), then compose.

        When a synthesiser is configured its network call is awaited directly
        on the event loop - never inside the worker thread, which is shared
        across every concurrent meeting the process is running and must not be
        blocked by an outbound API call. See :meth:`_compose_async`.
        """
        result = await self._retriever.retrieve(query)
        self.last_result = result
        if not result.grounded:
            return None
        composed = await self._compose_async(query, result)
        if composed is None:
            return None
        return self._to_grounded_answer(composed, topic, result)

    async def _compose_async(self, query: str, result: RetrievalResult) -> ComposedAnswer | None:
        """Prefer the LLM synthesiser over the full document text; fall back to the
        extractive composer (small chunks only) on decline, failure, or no synthesiser."""
        if self._synthesiser is not None:
            context = await self._retriever.store.run(
                lambda: _load_full_document_context(result, self._retriever.store.settings)
            )
            if context:
                try:
                    composed = await self._synthesiser.synthesise(query, context, result)
                    if composed is not None:
                        return composed
                except SynthesisError as exc:
                    logger.warning("LLM answer synthesis failed (%s); falling back to extractive composer", exc)
            else:
                logger.warning("could not load full document context for synthesis; falling back to extractive composer")
        return await self._retriever.store.run(lambda: self._composer.compose(query, result))

    @staticmethod
    def _to_grounded_answer(composed: ComposedAnswer, topic: str, result: RetrievalResult) -> GroundedAnswer:
        """Wrap a composed answer as a brain-facing :class:`GroundedAnswer`."""
        chunk = composed.chunk
        return GroundedAnswer(
            answer=composed.text,
            grounding=Grounding(
                source=GroundingSource.DOCUMENTS,
                subject=chunk.title or chunk.source,
                similarity=composed.similarity,
                citations=composed.citations or [chunk.citation],
                detail=(
                    f"topic={topic}; composer={composed.composer}; chunk similarity {chunk.similarity:.2f}; "
                    f"{len(result.chunks)} passage(s) above {result.threshold:.2f}"
                ),
            ),
        )


def build_knowledge_source(store: KnowledgeStore, settings: KnowledgeSettings | None = None) -> tuple[KnowledgeSource, DocumentKnowledge]:
    """Standard wiring: curated profile facts first, then the document index.

    The document source uses :func:`build_synthesiser` for its answers: Claude
    when ``ANTHROPIC_API_KEY`` is set, the extractive composer otherwise (and
    as the automatic fallback on any synthesis failure).

    Returns the composite for the engine and the document source itself so
    callers can inspect :attr:`DocumentKnowledge.last_result`.
    """
    documents = DocumentKnowledge(KnowledgeRetriever(store, settings), synthesiser=build_synthesiser())
    return CompositeKnowledge([ProfileKnowledge(), documents]), documents
