"""
Optional LLM backstop for request detection.

The Policy Router Brain (:mod:`src.brain.policy_engine`) decides whether an
utterance is a request using a deliberately small, explicit phrase lexicon
(``_REQUEST_MARKERS``) so every decision can be explained by pointing at a
phrase in that file. That lexicon cannot cover every way a real question gets
phrased, and a missed request means the delegate silently declines to answer
something a human clearly asked.

:class:`ClaudeIntentClassifier` (Anthropic) and :class:`GeminiIntentClassifier`
(Google) are narrow, single-purpose backstops: given an utterance the lexicon
did not recognise as a request, ask an LLM whether it actually is one. Neither
ever raises - on any failure (no key, network error, API error, refusal,
malformed output) they return ``False``, which reproduces exactly the verdict
the deterministic lexicon already gave. There is no separate "fallback
implementation" the way :mod:`src.memory.post_meeting` has one, because
``False`` already *is* the fallback.

:func:`build_intent_classifier` prefers Gemini when ``GEMINI_API_KEY`` is
set (override with ``DELEG8_LLM_PROVIDER=claude``), then Claude, then falls back to ``None``
(lexicon-only) - so the same code runs whichever provider you actually have
credentials for, and automatically upgrades if you add the other one later.

Used only from :meth:`PolicyEngine.evaluate_async`, and only for utterances
where a ``True`` verdict could actually change the routing outcome - see
:meth:`PolicyEngine._worth_checking`, which also bounds the call volume with
an interrogative-word pre-filter so ordinary declarative meeting chatter does
not trigger a call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Protocol, runtime_checkable

import anthropic
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from src.llm import choose_provider

logger = logging.getLogger("deleg8.brain.intent")

DEFAULT_INTENT_MODEL: str = "claude-haiku-4-5-20251001"
"""Fast, cheap Claude model: this sits in the live turn loop, not a background job."""
DEFAULT_GEMINI_INTENT_MODEL: str = "gemini-3.5-flash-lite"
"""Fast, cheap Gemini model. The previous default, ``gemini-2.5-flash-lite``, was
retired and returns 404 - which this classifier swallowed as "not a request",
silently reverting request detection to the lexicon alone. :mod:`src.llm` owns
model selection for everything on the live path now; this constant survives only
for the legacy ``PolicyEngine.evaluate_async`` route."""
INTENT_TIMEOUT_SECONDS: float = 3.0
GEMINI_MIN_CALL_INTERVAL_SECONDS: float = 0.5
"""Defensive local rate guard for the free tier: skip (return False) rather than
call again faster than this. Cheap insurance on top of PolicyEngine._worth_checking,
which already keeps normal call volume well under any published free-tier limit."""


class _IntentOutput(BaseModel):
    """Structured verdict asked of Claude."""

    is_request: bool = Field(
        ..., description="True when the utterance is a question or a request for information/action."
    )


_SYSTEM_PROMPT: str = """\
You are a component of a meeting delegate. You are given one utterance spoken \
in a live meeting. Decide only this: is the speaker asking a question or \
requesting information/action from someone, even if phrased indirectly and \
without a question mark (for example "walk me through the timeline" or \
"so what happened with the vendor")?

Answer False for statements, opinions, small talk, or anything that is not a \
request for a response."""


@runtime_checkable
class RequestIntentClassifier(Protocol):
    """Something that can judge whether an utterance is a request."""

    def describe(self) -> str:
        """One-line status for logs."""
        ...

    async def looks_like_request(self, utterance: str) -> bool:
        """True when ``utterance`` reads as a question or a request. Never raises."""
        ...


class ClaudeIntentClassifier:
    """Asks Claude a single True/False question. Fails closed to ``False``."""

    def __init__(
        self,
        model: str = DEFAULT_INTENT_MODEL,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        timeout: float = INTENT_TIMEOUT_SECONDS,
    ) -> None:
        """``client`` can be injected for tests; otherwise credentials come from the environment."""
        self._model = model
        self._client = client or anthropic.AsyncAnthropic(timeout=timeout)

    def describe(self) -> str:
        """Label."""
        return f"claude ({self._model})"

    async def looks_like_request(self, utterance: str) -> bool:
        """Ask Claude; return ``False`` on any failure instead of raising."""
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": f"Utterance: {utterance!r}"}],
                output_format=_IntentOutput,
            )
        except (
            anthropic.AuthenticationError,
            anthropic.RateLimitError,
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
        ) as exc:
            logger.warning("intent classifier unavailable (%s); treating as not a request", exc)
            return False

        if response.stop_reason == "refusal" or response.parsed_output is None:
            logger.warning("intent classifier declined to answer (stop_reason=%s)", response.stop_reason)
            return False
        return response.parsed_output.is_request


class GeminiIntentClassifier:
    """Asks Gemini a single True/False question. Fails closed to ``False``."""

    def __init__(
        self,
        model: str = DEFAULT_GEMINI_INTENT_MODEL,
        client: genai.Client | None = None,
        *,
        timeout: float = INTENT_TIMEOUT_SECONDS,
        min_call_interval: float = GEMINI_MIN_CALL_INTERVAL_SECONDS,
    ) -> None:
        """``client`` can be injected for tests; otherwise credentials come from the environment."""
        self._model = model
        self._client = client or genai.Client()
        self._timeout = timeout
        self._min_call_interval = min_call_interval
        self._last_call_at: float | None = None

    def describe(self) -> str:
        """Label."""
        return f"gemini ({self._model})"

    async def looks_like_request(self, utterance: str) -> bool:
        """Ask Gemini; return ``False`` on any failure, refusal, or local rate guard instead of raising."""
        now = time.monotonic()
        if self._last_call_at is not None and now - self._last_call_at < self._min_call_interval:
            logger.info("intent classifier: local rate guard hit; treating as not a request")
            return False
        self._last_call_at = now

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=f"Utterance: {utterance!r}",
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_schema=_IntentOutput,
                    ),
                ),
                timeout=self._timeout,
            )
        except genai_errors.APIError as exc:
            logger.warning("intent classifier unavailable (gemini %s: %s); treating as not a request", exc.code, exc.message)
            return False
        except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
            logger.warning("intent classifier unreachable (%s); treating as not a request", exc)
            return False

        if response.parsed is None:
            finish = response.candidates[0].finish_reason if response.candidates else None
            logger.warning("intent classifier declined to answer (finish_reason=%s)", finish)
            return False
        return response.parsed.is_request


def build_intent_classifier() -> RequestIntentClassifier | None:
    """Gemini when ``GEMINI_API_KEY`` is set (the default); else Claude when ``ANTHROPIC_API_KEY``
    is set; else ``None`` (lexicon-only). See :func:`src.llm.choose_provider`."""
    provider = choose_provider(
        claude=bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        gemini=bool(os.getenv("GEMINI_API_KEY", "").strip()),
    )
    if provider == "gemini":
        model = os.getenv("DELEG8_GEMINI_INTENT_MODEL", "").strip() or DEFAULT_GEMINI_INTENT_MODEL
        logger.info("intent classifier: gemini (%s)", model)
        return GeminiIntentClassifier(model)
    if provider == "claude":
        model = os.getenv("DELEG8_INTENT_MODEL", "").strip() or DEFAULT_INTENT_MODEL
        logger.info("intent classifier: claude (%s)", model)
        return ClaudeIntentClassifier(model)
    logger.info("intent classifier: none (no GEMINI_API_KEY or ANTHROPIC_API_KEY); lexicon only")
    return None
