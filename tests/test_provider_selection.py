"""
Standalone verification that the "best available provider wins" precedence
is wired correctly across the LLM-backed components (Sprint 8: Claude, then
Gemini, then a deterministic fallback), and a dedicated check of the
citation-verification safety net (src/knowledge/grounding.py) both pieces
of answer synthesis rely on.

``build_synthesiser()``'s own precedence is covered alongside its fixture in
tests/test_answer_synthesis.py; this file covers the other two factories
(``build_summariser``, ``build_intent_classifier``) plus ``verify_citations``
in isolation.

Fully offline: no real API key, no network call, no mocking library (none is
installed in this repo) - just ``os.environ`` save/restore, matching every
other standalone script under tests/.

Usage::

    python tests/test_provider_selection.py

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brain.intent import (
    ClaudeIntentClassifier,
    GeminiIntentClassifier,
    build_intent_classifier,
)
from src.knowledge.grounding import verify_citations
from src.llm import AnthropicLlm, GeminiLlm, ModelRole, build_llm
from src.memory.post_meeting import (
    LlmSummariser,
    TemplateSummariser,
    build_summariser,
)

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


def _with_keys(anthropic_key: str | None, gemini_key: str | None):
    """Context manager: set exactly these two env vars, restore whatever was there after."""

    class _Ctx:
        def __enter__(self) -> None:
            self._saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")}
            if anthropic_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = anthropic_key
            if gemini_key is not None:
                os.environ["GEMINI_API_KEY"] = gemini_key

        def __exit__(self, *_exc: object) -> None:
            for key, value in self._saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value

    return _Ctx()


def _with_env(name: str, value: str):
    """Context manager: set one env var, restore it after."""

    class _Ctx:
        def __enter__(self) -> None:
            self._saved = os.environ.get(name)
            os.environ[name] = value

        def __exit__(self, *_exc: object) -> None:
            if self._saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = self._saved

    return _Ctx()


def check_build_llm_precedence() -> None:
    """Provider selection now lives in one place (src/llm.py) instead of once per component.

    Each component used to build its own client and hardcode its own model id,
    which is how one retired id (``gemini-2.5-flash``) disabled answer synthesis,
    intent detection and the briefing at once without anything reporting it.
    """
    print("\n1. build_llm() provider precedence")
    with _with_keys(None, None):
        _check("neither key -> None (no brain)", build_llm() is None)
    with _with_keys(None, "fake-gemini-key"):
        client = build_llm()
        _check("Gemini key only -> GeminiLlm", isinstance(client, GeminiLlm), repr(client))
    with _with_keys("fake-anthropic-key", "fake-gemini-key"):
        client = build_llm()
        _check("both keys -> Gemini wins (the default)", isinstance(client, GeminiLlm), repr(client))
    with _with_keys("fake-anthropic-key", None):
        client = build_llm()
        _check("Anthropic key only -> AnthropicLlm", isinstance(client, AnthropicLlm), repr(client))
    with _with_keys("fake-anthropic-key", "fake-gemini-key"), _with_env("DELEG8_LLM_PROVIDER", "claude"):
        client = build_llm()
        _check("DELEG8_LLM_PROVIDER=claude flips the preference", isinstance(client, AnthropicLlm), repr(client))
    with _with_keys(None, "fake-gemini-key"), _with_env("DELEG8_LLM_PROVIDER", "claude"):
        client = build_llm()
        _check("preferred provider without a key yields to the one that has one", isinstance(client, GeminiLlm), repr(client))

    with _with_keys(None, "fake-gemini-key"):
        client = build_llm()
        assert client is not None
        for role in ModelRole:
            chain = client.models_for(role)
            _check(f"{role.value} role has a fallback chain", len(chain) >= 2, repr(chain))
            _check(
                f"{role.value} chain contains no retired 2.5 models",
                not any(m.startswith("gemini-2.5") for m in chain),
                repr(chain),
            )


def check_build_summariser_precedence() -> None:
    print("\n1b. build_summariser() provider precedence")
    with _with_keys(None, None):
        _check("neither key -> TemplateSummariser", isinstance(build_summariser(), TemplateSummariser))
    with _with_keys(None, "fake-gemini-key"):
        summariser = build_summariser()
        _check("Gemini key only -> LlmSummariser", isinstance(summariser, LlmSummariser), repr(summariser))
    with _with_keys("fake-anthropic-key", "fake-gemini-key"):
        summariser = build_summariser()
        _check("both keys -> LlmSummariser (Gemini underneath)", isinstance(summariser, LlmSummariser) and summariser.describe().startswith("gemini"), repr(summariser.describe()))


def check_build_intent_classifier_precedence() -> None:
    print("\n2. build_intent_classifier() provider precedence")
    with _with_keys(None, None):
        _check("neither key -> None (lexicon only)", build_intent_classifier() is None)
    with _with_keys(None, "fake-gemini-key"):
        classifier = build_intent_classifier()
        _check("Gemini key only -> GeminiIntentClassifier", isinstance(classifier, GeminiIntentClassifier), repr(classifier))
    with _with_keys("fake-anthropic-key", "fake-gemini-key"):
        classifier = build_intent_classifier()
        _check("both keys -> Gemini wins (the default)", isinstance(classifier, GeminiIntentClassifier), repr(classifier))
    with _with_keys("fake-anthropic-key", "fake-gemini-key"), _with_env("DELEG8_LLM_PROVIDER", "claude"):
        classifier = build_intent_classifier()
        _check("DELEG8_LLM_PROVIDER=claude -> Claude", isinstance(classifier, ClaudeIntentClassifier), repr(classifier))


_SOURCE_TEXT: str = (
    "Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database.\n"
    "The frontend is a React single-page application. “Quoted” text uses curly quotes here."
)


def check_verify_citations() -> None:
    print("\n3. verify_citations()")
    _check(
        "exact verbatim match",
        verify_citations(["Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database."], _SOURCE_TEXT),
    )
    _check(
        "whitespace-normalised match (extra spaces/newline in the quote)",
        verify_citations(["Project  Alpha uses PostgreSQL 16\non Amazon RDS as its primary database."], _SOURCE_TEXT),
    )
    _check(
        "smart-quote-normalised match (straight quotes in the quote, curly in the source)",
        verify_citations(['"Quoted" text uses curly quotes here.'], _SOURCE_TEXT),
    )
    _check(
        "multiple citations, all real, all verify",
        verify_citations(
            ["Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database.",
             "The frontend is a React single-page application."],
            _SOURCE_TEXT,
        ),
    )
    _check(
        "fabricated citation (not in the source) fails",
        not verify_citations(["The backend runs on a mainframe from 1985."], _SOURCE_TEXT),
    )
    _check(
        "one real + one fabricated citation fails (all-or-nothing)",
        not verify_citations(
            ["Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database.", "invented, not in the text"],
            _SOURCE_TEXT,
        ),
    )
    _check("empty citation list fails closed", not verify_citations([], _SOURCE_TEXT))


def main() -> int:
    print("DELEG8 provider-selection and citation-verification: standalone verification")
    check_build_llm_precedence()
    check_build_summariser_precedence()
    check_build_intent_classifier_precedence()
    check_verify_citations()
    print(f"\n{_PASSED} passed, {_FAILED} failed")
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
