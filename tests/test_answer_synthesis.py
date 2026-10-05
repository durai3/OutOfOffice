"""
Standalone verification for the LLM answer synthesisers (Claude and Gemini),
the citation-verification safety net, and the full-document RAG context
redesign (Sprint 8).

Fully offline: no real API key, no network call. Hand-rolled fake clients
(mirroring the injectable ``client`` seam :class:`~src.memory.post_meeting.ClaudeSummariser`
already uses) stand in for ``anthropic.AsyncAnthropic`` and ``genai.Client``,
scripted to return canned responses or raise the same exception types the
real SDKs raise.

What it checks, against a *real* :class:`~src.knowledge.retriever.RetrievalResult`
(the sample document is ingested into the real ChromaDB index, same as
tests/test_memory_rag.py), for BOTH providers:

    1. A successful synthesis with a verbatim, verifiable quote returns a
       ComposedAnswer with ``composer`` naming the model and ``citations``
       covering every supplied passage.
    2. ``answered=False`` returns None (declined, not an error).
    3. A fabricated (non-verbatim) ``supporting_quotes`` entry also returns
       None - the citation-verification safety net catching an unsupported
       claim regardless of how confident the model sounded.
    4. Every technical failure the real SDKs can raise (auth, rate limit,
       API status, connection/timeout) raises SynthesisError.
    5. For every case in (2)-(4), DocumentKnowledge.lookup_async's internal
       composer step falls back to exactly what the plain extractive
       AnswerComposer would have produced on its own (the oracle).
    6. A slow fake call does not block a concurrent CPU-bound call through
       the same KnowledgeStore worker thread (the lookup_async threading fix).
    7. build_synthesiser() provider precedence and the no-key fallback.
    8. The full-document context fix: a fact placed in a section of a longer
       document that today's top-k chunk retrieval would NOT surface is
       still present in what the synthesiser actually reads.

Usage::

    python tests/test_answer_synthesis.py

Exit codes: 0 = every check passed, 1 = at least one failed, 2 = configuration problem.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import anthropic
import httpx
from dotenv import load_dotenv
from google.genai import errors as genai_errors

load_dotenv(PROJECT_ROOT / ".env")

from src.knowledge.config import KnowledgeConfigError, KnowledgeSettings
from src.knowledge.ingest import DocumentIngestor
from src.knowledge.retriever import (
    AnswerComposer,
    ClaudeAnswerSynthesiser,
    DocumentKnowledge,
    GeminiAnswerSynthesiser,
    KnowledgeRetriever,
    RetrievalResult,
    SynthesisError,
    _AnswerOutput,
    _load_full_document_context,
    build_synthesiser,
)
from src.knowledge.store import KnowledgeStore
from tests.test_memory_rag import DOCUMENT_NAME, SAMPLE_DOCUMENT

_PASSED = 0
_FAILED = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [✓] {label}")
    else:
        _FAILED += 1
        print(f"  [✗] {label}{f' — {detail}' if detail else ''}")


QUERY: str = "What database is Project Alpha using?"
_VERBATIM_QUOTE: str = (
    "Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database, "
    "with Redis 7 for caching and session storage."
)
"""Copied character-for-character from SAMPLE_DOCUMENT's Architecture section."""

_REQUEST = httpx.Request("POST", "https://api.example.com/v1/messages")


# --------------------------------------------------------------------------- #
# Fake Anthropic client: no network, scripted responses/exceptions.
# --------------------------------------------------------------------------- #

def _claude_response(text: str, *, answered: bool = True, quotes: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        stop_details=None,
        parsed_output=_AnswerOutput(
            answered=answered, answer=text if answered else "",
            supporting_quotes=quotes if quotes is not None else ([_VERBATIM_QUOTE] if answered else []),
        ),
        usage=SimpleNamespace(input_tokens=42, output_tokens=7),
        _request_id="req_fake",
    )


def _claude_refusal_response() -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="refusal", stop_details=SimpleNamespace(explanation="policy"), parsed_output=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=0), _request_id="req_fake",
    )


def _claude_no_parsed_output_response() -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="max_tokens", stop_details=None, parsed_output=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=0), _request_id="req_fake",
    )


@dataclass
class FakeMessages:
    """Stands in for ``client.messages``."""

    behavior: object
    calls: int = 0

    async def parse(self, **_kwargs: object) -> object:
        self.calls += 1
        behavior = self.behavior
        if isinstance(behavior, tuple):
            delay, response = behavior
            await asyncio.sleep(delay)
            return response
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


@dataclass
class FakeAnthropicClient:
    """Stands in for ``anthropic.AsyncAnthropic``."""

    behavior: object
    messages: FakeMessages = field(init=False)

    def __post_init__(self) -> None:
        self.messages = FakeMessages(self.behavior)


def _claude_synthesiser(behavior: object) -> ClaudeAnswerSynthesiser:
    return ClaudeAnswerSynthesiser(client=FakeAnthropicClient(behavior))


def _claude_exceptions() -> dict[str, BaseException]:
    return {
        "AuthenticationError": anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=_REQUEST, json={}), body=None
        ),
        "RateLimitError": anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=_REQUEST, json={}), body=None
        ),
        "APIStatusError": anthropic.APIStatusError(
            "server error", response=httpx.Response(500, request=_REQUEST, json={}), body=None
        ),
        "APIConnectionError": anthropic.APIConnectionError(message="no network", request=_REQUEST),
    }


# --------------------------------------------------------------------------- #
# Fake Gemini client: no network, scripted responses/exceptions.
# --------------------------------------------------------------------------- #

def _gemini_response(text: str, *, answered: bool = True, quotes: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        parsed=_AnswerOutput(
            answered=answered, answer=text if answered else "",
            supporting_quotes=quotes if quotes is not None else ([_VERBATIM_QUOTE] if answered else []),
        ),
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(prompt_token_count=42, candidates_token_count=7),
    )


def _gemini_no_parsed_response() -> SimpleNamespace:
    return SimpleNamespace(
        parsed=None, candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")], usage_metadata=None,
    )


@dataclass
class FakeGeminiModels:
    """Stands in for ``client.aio.models``."""

    behavior: object
    calls: int = 0

    async def generate_content(self, **_kwargs: object) -> object:
        self.calls += 1
        behavior = self.behavior
        if isinstance(behavior, tuple):
            delay, response = behavior
            await asyncio.sleep(delay)
            return response
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior


@dataclass
class FakeGeminiAio:
    models: FakeGeminiModels


@dataclass
class FakeGeminiClient:
    """Stands in for ``genai.Client``."""

    behavior: object
    aio: FakeGeminiAio = field(init=False)

    def __post_init__(self) -> None:
        self.aio = FakeGeminiAio(models=FakeGeminiModels(self.behavior))


def _gemini_synthesiser(behavior: object) -> GeminiAnswerSynthesiser:
    return GeminiAnswerSynthesiser(client=FakeGeminiClient(behavior))


def _gemini_exceptions() -> dict[str, BaseException]:
    return {
        "ClientError (401)": genai_errors.ClientError(
            401, {"error": {"message": "bad key"}}, response=httpx.Response(401, request=_REQUEST, json={})
        ),
        "ClientError (429)": genai_errors.ClientError(
            429, {"error": {"message": "rate limited"}}, response=httpx.Response(429, request=_REQUEST, json={})
        ),
        "ServerError (500)": genai_errors.ServerError(
            500, {"error": {"message": "server error"}}, response=httpx.Response(500, request=_REQUEST, json={})
        ),
        "ConnectError": httpx.ConnectError("no network"),
        "TimeoutException": httpx.TimeoutException("timed out"),
    }


# --------------------------------------------------------------------------- #
# Provider-agnostic test kit
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProviderKit:
    """Everything a check needs to exercise one provider's synthesiser, generically."""

    name: str
    make_synthesiser: object  # Callable[[object], AnswerSynthesiser]
    make_success: object  # Callable[..., object]
    make_no_output: object  # Callable[[], object]
    exceptions: dict[str, BaseException]


CLAUDE_KIT = ProviderKit(
    name="claude", make_synthesiser=_claude_synthesiser, make_success=_claude_response,
    make_no_output=_claude_no_parsed_output_response, exceptions=_claude_exceptions(),
)
GEMINI_KIT = ProviderKit(
    name="gemini", make_synthesiser=_gemini_synthesiser, make_success=_gemini_response,
    make_no_output=_gemini_no_parsed_response, exceptions=_gemini_exceptions(),
)
KITS: tuple[ProviderKit, ...] = (CLAUDE_KIT, GEMINI_KIT)


# --------------------------------------------------------------------------- #
# Fixture: a real RetrievalResult (and its full-document context) from the real index.
# --------------------------------------------------------------------------- #

async def build_fixture() -> tuple[KnowledgeStore, KnowledgeSettings, RetrievalResult, str]:
    settings = KnowledgeSettings.from_env()
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    (settings.documents_dir / DOCUMENT_NAME).write_text(SAMPLE_DOCUMENT, encoding="utf-8")
    store = KnowledgeStore(settings)
    await store.open_async()
    await DocumentIngestor(store, settings).ingest_directory()
    retriever = KnowledgeRetriever(store, settings)
    result = await retriever.retrieve(QUERY)
    if not result.grounded:
        raise RuntimeError(f"fixture is not grounded (best={result.best_similarity}); is {DOCUMENT_NAME} ingested?")
    context = await store.run(lambda: _load_full_document_context(result, settings))
    return store, settings, result, context


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

async def check_success_and_decline(result: RetrievalResult, context: str) -> None:
    print("\n1-3. Synthesis outcomes: success / declined / unverified citation")
    for kit in KITS:
        synth = kit.make_synthesiser(kit.make_success("Project Alpha uses PostgreSQL for its primary database."))
        composed = await synth.synthesise(QUERY, context, result)
        _check(f"[{kit.name}] successful synthesis returns a ComposedAnswer", composed is not None)
        if composed is not None:
            _check(f"[{kit.name}] answer text matches the scripted response", "PostgreSQL" in composed.text, composed.text)
            _check(f"[{kit.name}] composer names the model", composed.composer.startswith(f"{kit.name} ("), composed.composer)
            _check(
                f"[{kit.name}] citations cover every supplied passage",
                composed.citations == result.citations(),
                f"{composed.citations} != {result.citations()}",
            )

        synth = kit.make_synthesiser(kit.make_success("", answered=False))
        composed = await synth.synthesise(QUERY, context, result)
        _check(f"[{kit.name}] answered=False returns None (declined, not an error)", composed is None)

        synth = kit.make_synthesiser(
            kit.make_success("Project Alpha uses PostgreSQL.", quotes=["this sentence does not appear anywhere in the document"])
        )
        composed = await synth.synthesise(QUERY, context, result)
        _check(f"[{kit.name}] fabricated (non-verbatim) quote returns None, not a ComposedAnswer", composed is None)


async def check_failures_fall_back(store: KnowledgeStore, result: RetrievalResult, context: str) -> None:
    print("\n4-5. Every technical-failure path raises SynthesisError and falls back to the extractive composer")
    oracle = AnswerComposer(store, min_similarity=store.settings.min_similarity).compose(QUERY, result)
    assert oracle is not None, "extractive composer must ground this fixture query too"

    for kit in KITS:
        cases: dict[str, object] = {**kit.exceptions, "no structured output": kit.make_no_output()}
        for label, behavior in cases.items():
            synth = kit.make_synthesiser(behavior)
            raised = False
            try:
                await synth.synthesise(QUERY, context, result)
            except SynthesisError:
                raised = True
            _check(f"[{kit.name}] {label} raises SynthesisError", raised)

            documents = DocumentKnowledge(KnowledgeRetriever(store, store.settings), synthesiser=synth)
            composed = await documents._compose_async(QUERY, result)
            _check(
                f"[{kit.name}] {label}: DocumentKnowledge falls back to the extractive answer",
                composed is not None and composed.text == oracle.text and composed.composer == "extractive",
                f"got {composed!r}",
            )


async def check_concurrency_not_blocked(store: KnowledgeStore, result: RetrievalResult) -> None:
    print("\n6. A slow synthesis call does not block the shared worker thread")
    for kit in KITS:
        slow_response = (0.4, kit.make_success("Project Alpha uses PostgreSQL."))
        synth = kit.make_synthesiser(slow_response)
        documents = DocumentKnowledge(KnowledgeRetriever(store, store.settings), synthesiser=synth)

        async def cpu_bound_probe() -> float:
            start = time.monotonic()
            await store.run(store.count)
            return time.monotonic() - start

        slow_task = asyncio.ensure_future(documents._compose_async(QUERY, result))
        await asyncio.sleep(0.05)  # let the slow call actually start first
        probe_elapsed = await cpu_bound_probe()
        await slow_task  # drain it so it doesn't warn on interpreter shutdown
        _check(
            f"[{kit.name}] CPU-bound call finished well before the slow synthesis call",
            probe_elapsed < 0.35,
            f"probe took {probe_elapsed:.3f}s (should be near-instant, not ~0.4s)",
        )


def check_build_synthesiser_precedence() -> None:
    print("\n7. build_synthesiser() provider precedence")
    saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")}
    try:
        _check("neither key set -> None (extractive composer only)", build_synthesiser() is None)

        os.environ["GEMINI_API_KEY"] = "fake-gemini-key"
        synth = build_synthesiser()
        _check("only GEMINI_API_KEY set -> GeminiAnswerSynthesiser", isinstance(synth, GeminiAnswerSynthesiser), repr(synth))

        os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic-key"
        synth = build_synthesiser()
        _check("both keys set -> Claude wins", isinstance(synth, ClaudeAnswerSynthesiser), repr(synth))
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


async def check_full_document_context() -> None:
    print("\n8. Full-document context: a fact outside today's top-k chunk window is still readable")
    settings = KnowledgeSettings.from_env()
    settings.documents_dir.mkdir(parents=True, exist_ok=True)
    name = "long_test_doc.md"
    trivia_sentence = "The project's internal codename was chosen by rolling a twenty-sided die at the kickoff meeting."
    sections = [
        ("Overview", "This document describes a large internal platform used by several teams across the company."),
        ("Database", "The platform stores its primary data in a managed PostgreSQL cluster with automated backups."),
        ("Caching layer", "A Redis cluster caches session data and frequently accessed lookup tables for the platform."),
        ("Backend services", "The backend is a set of Python microservices communicating over gRPC and deployed on Kubernetes."),
        ("Frontend application", "The frontend is a TypeScript single-page application built with a modern component framework."),
        ("Deployment pipeline", "Continuous integration runs the full test suite on every pull request before merging to main."),
        ("Monitoring and alerting", "Metrics are collected centrally and alerts page the on-call engineer for any critical failure."),
        ("Historical trivia", trivia_sentence),
    ]
    body = "\n\n".join(f"## {title}\n\n{text}" for title, text in sections)
    (settings.documents_dir / name).write_text(f"# Large Test Platform\n\n{body}\n", encoding="utf-8")

    store = KnowledgeStore(settings)
    try:
        await store.open_async()
        report = await DocumentIngestor(store, settings).ingest_directory(force=True)
        ours = next((f for f in report.files if f.source == name), None)
        _check("long test document ingested with more chunks than top_k", ours is not None and ours.chunks > settings.top_k, str(ours))

        retriever = KnowledgeRetriever(store, settings)
        query = "What database technology does the platform use?"
        result = await retriever.retrieve(query)
        _check("query is grounded", result.grounded, f"best={result.best_similarity}")

        in_top_k_chunks = any(trivia_sentence in chunk.text for chunk in result.chunks)
        _check(
            "sanity check: the trivia fact is NOT in the top-k retrieved chunks for this query",
            not in_top_k_chunks,
            "test document/query no longer demonstrates the gap - adjust the fixture",
        )

        context = await store.run(lambda: _load_full_document_context(result, settings))
        _check(
            "the full-document context DOES include the trivia fact anyway",
            trivia_sentence in context,
            "full-document loader did not include the whole source document",
        )
    finally:
        store.close()
        (settings.documents_dir / name).unlink(missing_ok=True)


async def main_async() -> int:
    print("DELEG8 answer-synthesis fix: standalone verification (Claude + Gemini)")
    try:
        store, _settings, result, context = await build_fixture()
    except KnowledgeConfigError as exc:
        print(f"\nConfiguration problem: {exc}")
        return 2

    try:
        await check_success_and_decline(result, context)
        await check_failures_fall_back(store, result, context)
        await check_concurrency_not_blocked(store, result)
        check_build_synthesiser_precedence()
        await check_full_document_context()
    finally:
        store.close()

    print(f"\n{_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
