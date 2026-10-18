"""
One place that knows how to talk to a language model.

Before this module existed, three components (:mod:`src.brain.intent`,
:mod:`src.knowledge.retriever`, :mod:`src.memory.post_meeting`) each built
their own client, each hardcoded their own model name, and each caught every
failure and silently degraded to a deterministic fallback. That combination
is how a dead model id went unnoticed through an entire live meeting: the
product kept answering, from the extractive fallback, and nothing anywhere
said the brain was off.

So this module owns three things:

* **Model selection.** :data:`ANTHROPIC_MODELS` / :data:`GEMINI_MODELS` map a
  :class:`ModelRole` to a real model id. Two roles only: :attr:`ModelRole.FAST`
  sits in the live turn loop (latency is the product), :attr:`ModelRole.SMART`
  runs off the critical path (dossier build, briefing) where quality wins.
* **One call shape.** :meth:`LlmClient.generate` takes a system prompt, a user
  prompt and a Pydantic schema, and returns a validated instance of it.
  Provider differences (``messages.parse`` vs ``generate_content`` with a
  response schema, thinking budgets, timeout handling) stop here.
* **Liveness.** :meth:`LlmClient.preflight` makes one real, cheap call per
  configured model. :func:`preflight_or_raise` is called at startup by every
  entry point, so a dead model id is a loud startup failure, not a silent
  quality collapse discovered days later in a JSON archive.

Failures raise :class:`LlmError`. Callers decide whether to degrade, and when
they do they must record it (see :class:`~src.health.Degradation`) so the
console can say so out loud.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar, runtime_checkable

import anthropic
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel

logger = logging.getLogger("deleg8.llm")

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    """Any failure to get a usable structured answer out of a model."""


class LlmUnavailable(LlmError):
    """The provider is not configured, or a configured model does not exist.

    Distinct from a transient :class:`LlmError` because this one will not fix
    itself by retrying: it means the deployment is misconfigured.
    """


class LlmRateLimited(LlmError):
    """The provider refused for quota reasons (HTTP 429).

    Worth separating because the right response differs from other transient
    errors: waiting milliseconds does nothing against a per-minute quota, so
    the chain moves to another model rather than hammering this one.
    """


class LlmDeclined(LlmError):
    """The model refused, or returned nothing parseable."""


class ModelRole(str, Enum):
    """What a model is being asked to do, which decides how it is configured."""

    FAST = "fast"
    """In the live turn loop (floor triage, the response planner). Thinking is
    disabled and the timeout is short: a late answer in a meeting is a failure
    even when it is a good answer."""

    SMART = "smart"
    """Off the critical path (dossier compilation, the post-meeting briefing).
    Thinking stays on and the timeout is generous."""


# --------------------------------------------------------------------------- #
# Model registry
# --------------------------------------------------------------------------- #

ANTHROPIC_MODELS: dict[ModelRole, tuple[str, ...]] = {
    ModelRole.FAST: ("claude-haiku-4-5-20251001", "claude-sonnet-5"),
    ModelRole.SMART: ("claude-sonnet-5", "claude-opus-5"),
}

GEMINI_MODELS: dict[ModelRole, tuple[str, ...]] = {
    ModelRole.FAST: ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-flash-lite-latest"),
    ModelRole.SMART: (
        "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-flash-latest",
    ),
}
"""Primary model first, then fallbacks tried in order.

Two failure modes make the chain worth having, and both were observed while
building this: a model can be *retired* (``gemini-2.5-flash``, the previous
default, now returns 404 "no longer available to new users" - the bug that made
the delegate quote PDF fragments for a whole meeting), and a model can be
*briefly overloaded* (503 "experiencing high demand"). A retired model is
skipped permanently for the call; an overloaded one is retried with backoff
before the chain moves on."""

ROLE_TIMEOUTS: dict[ModelRole, float] = {
    ModelRole.FAST: 8.0,
    ModelRole.SMART: 120.0,
}

ROLE_ATTEMPTS: dict[ModelRole, int] = {
    ModelRole.FAST: 2,
    ModelRole.SMART: 3,
}
"""Attempts per model before moving to the next in the chain. The live path gets
fewer: in a meeting, a fast wrong-shaped failure that falls through to the next
model beats a slow correct one."""

_BACKOFF_SECONDS: float = 0.75
_RATE_LIMIT_BACKOFF_SECONDS: float = 4.0
"""Quota windows are measured in minutes, not milliseconds, so a 429 gets a much
longer wait than an ordinary transient error - and on the live path is usually
better spent falling through to the next model in the chain."""
_FATAL_STATUS: frozenset[int] = frozenset({401, 403, 404})
_RATE_LIMIT_STATUS: int = 429

_ENV_OVERRIDES: dict[tuple[str, ModelRole], str] = {
    ("anthropic", ModelRole.FAST): "DELEG8_ANTHROPIC_FAST_MODEL",
    ("anthropic", ModelRole.SMART): "DELEG8_ANTHROPIC_SMART_MODEL",
    ("gemini", ModelRole.FAST): "DELEG8_GEMINI_FAST_MODEL",
    ("gemini", ModelRole.SMART): "DELEG8_GEMINI_SMART_MODEL",
}


def strict_mode() -> bool:
    """True when ``DELEG8_STRICT_LLM`` forbids degrading to a non-LLM fallback.

    Off by default so a transient outage cannot cost you a live meeting; turn
    it on in development and in CI, where a silent fallback is a bug you want
    to see immediately.
    """
    return os.getenv("DELEG8_STRICT_LLM", "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Client protocol
# --------------------------------------------------------------------------- #

@runtime_checkable
class LlmClient(Protocol):
    """Structured generation against one provider."""

    provider: str

    def describe(self) -> str:
        """One-line label naming the provider and both models."""
        ...

    def model_for(self, role: ModelRole) -> str:
        """The primary model id this client will use for ``role``."""
        ...

    def models_for(self, role: ModelRole) -> tuple[str, ...]:
        """The full fallback chain for ``role``, primary first."""
        ...

    async def generate(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        role: ModelRole = ModelRole.FAST,
        timeout: float | None = None,
        max_output_tokens: int = 2048,
    ) -> T:
        """Return a validated ``schema`` instance.

        Raises:
            LlmUnavailable: no model in the chain exists or credentials are refused.
            LlmDeclined: the model refused or returned nothing parseable.
            LlmError: any other failure (network, rate limit, timeout).
        """
        ...

    on_usage: "UsageCallback | None"
    """Optional per-call token sink; see :data:`UsageCallback`."""

    async def preflight(self) -> None:
        """Make one real call per role, walking the fallback chain.

        Raises:
            LlmUnavailable: if a role has no usable model at all.
        """
        ...


class _Ok(BaseModel):
    """Trivial schema used by :meth:`LlmClient.preflight`."""

    ok: bool


class UsageReport(BaseModel):
    """Tokens consumed by one model call, as the provider reported them."""

    provider: str
    model: str
    role: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


UsageCallback = Callable[[UsageReport], None]
"""Receives one :class:`UsageReport` per successful model call.

This is the seam multi-tenant metering hangs off (:mod:`src.usage.meter`).
It is a callback rather than a return value because the token counts must be
attributed even when the caller only wanted the parsed answer, and because a
metering failure must never propagate into a live meeting turn - the emitter
swallows whatever the callback raises."""


class _ChainedLlm:
    """Shared retry-and-fallback loop; providers supply :meth:`_call_one`."""

    provider: str = "unknown"

    def __init__(self, models: dict[ModelRole, tuple[str, ...]]) -> None:
        """``models`` maps each role to its chain, primary first."""
        self._models = {role: tuple(chain) for role, chain in models.items()}
        self._retired: set[str] = set()
        """Models that returned a fatal status this process; skipped on later calls
        so one retirement is not re-discovered on every single turn."""
        self.on_usage: UsageCallback | None = None
        """Set by the owning tenant's :class:`~src.usage.meter.UsageMeter`."""

    def _report_usage(
        self, model: str, role: ModelRole, input_tokens: int, output_tokens: int, latency_ms: float
    ) -> None:
        """Hand one call's token counts to the meter, if one is attached."""
        callback = self.on_usage
        if callback is None:
            return
        try:
            callback(
                UsageReport(
                    provider=self.provider, model=model, role=role.value,
                    input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
                    latency_ms=latency_ms,
                )
            )
        except Exception:  # noqa: BLE001 - metering must never break a meeting turn
            logger.exception("usage callback failed for %s", model)

    def models_for(self, role: ModelRole) -> tuple[str, ...]:
        """The chain for ``role``, primary first."""
        return self._models[role]

    def model_for(self, role: ModelRole) -> str:
        """The first model in the chain that has not been retired this process."""
        for model in self._models[role]:
            if model not in self._retired:
                return model
        return self._models[role][0]

    def describe(self) -> str:
        """Label."""
        fast, smart = self.model_for(ModelRole.FAST), self.model_for(ModelRole.SMART)
        return f"{self.provider} (fast: {fast}, smart: {smart})"

    async def _call_one(
        self, model: str, *, system: str, user: str, schema: type[T], role: ModelRole,
        timeout: float, max_output_tokens: int,
    ) -> T:
        """One request to one model. Implemented per provider."""
        raise NotImplementedError

    async def generate(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        role: ModelRole = ModelRole.FAST,
        timeout: float | None = None,
        max_output_tokens: int = 2048,
    ) -> T:
        """Walk the chain for ``role``, retrying transient failures with backoff."""
        budget = timeout if timeout is not None else ROLE_TIMEOUTS[role]
        attempts = ROLE_ATTEMPTS[role]
        chain = [m for m in self._models[role] if m not in self._retired] or list(self._models[role])
        last: LlmError = LlmUnavailable(f"{self.provider}: no model configured for role {role.value}")

        for index, model in enumerate(chain):
            for attempt in range(attempts):
                try:
                    return await self._call_one(
                        model, system=system, user=user, schema=schema, role=role,
                        timeout=budget, max_output_tokens=max_output_tokens,
                    )
                except LlmUnavailable as exc:
                    # Retired or forbidden: never worth retrying, and worth remembering.
                    self._retired.add(model)
                    logger.warning("%s: retiring for this process (%s)", model, exc)
                    last = exc
                    break
                except LlmRateLimited as exc:
                    # A per-minute quota will not clear in the time a meeting turn
                    # has; try another model immediately instead of sleeping, and
                    # only wait when this is the last model available.
                    last = exc
                    if index + 1 < len(chain):
                        logger.warning("%s rate limited; falling straight through to %s", model, chain[index + 1])
                        break
                    if attempt + 1 < attempts:
                        logger.info("%s rate limited and last in chain; waiting %.1fs", model, _RATE_LIMIT_BACKOFF_SECONDS)
                        await asyncio.sleep(_RATE_LIMIT_BACKOFF_SECONDS)
                        continue
                    break
                except LlmError as exc:
                    last = exc
                    if attempt + 1 < attempts:
                        delay = _BACKOFF_SECONDS * (2**attempt)
                        logger.info("%s: %s; retrying in %.1fs", model, exc, delay)
                        await asyncio.sleep(delay)
                        continue
                    if index + 1 < len(chain):
                        logger.warning("%s exhausted (%s); falling back to %s", model, exc, chain[index + 1])
            continue
        raise last

    async def preflight(self) -> None:
        """One real call per role; a role passes if any model in its chain answers."""
        for role in ModelRole:
            await self.generate(
                system='Reply with {"ok": true}.',
                user="Health check.",
                schema=_Ok,
                role=role,
                timeout=45.0,
                max_output_tokens=512,
            )


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

class GeminiLlm(_ChainedLlm):
    """Google Gemini behind :class:`LlmClient`."""

    provider = "gemini"

    def __init__(
        self,
        models: dict[ModelRole, tuple[str, ...]] | None = None,
        client: genai.Client | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        """``client`` is injectable for tests; ``api_key`` overrides ``GEMINI_API_KEY``.

        The explicit key is what lets one process serve several tenants on
        several different keys at once: each tenant's client is constructed with
        their own, instead of every client reading one shared environment
        variable.
        """
        super().__init__(models or GEMINI_MODELS)
        self._client = client or (genai.Client(api_key=api_key) if api_key else genai.Client())
        self._no_thinking_control: set[str] = set()
        """Models that rejected an explicit thinking budget. ``gemini-3.6-flash``
        accepts ``thinking_budget=0``; ``gemini-3.5-flash-lite`` answers 400 for the
        same field. Rather than hardcode a compatibility table that goes stale with
        every model release, the first 400 teaches this client and the call is
        retried without it."""

    async def _call_one(
        self, model: str, *, system: str, user: str, schema: type[T], role: ModelRole,
        timeout: float, max_output_tokens: int,
    ) -> T:
        """One structured-output request, with thinking disabled for the FAST role."""
        # Measured on gemini-3.6-flash: thinking off takes a realistic grounded
        # reply from ~3.4s to ~1.7s with no loss of answer quality.
        want_thinking_control = role is ModelRole.FAST and model not in self._no_thinking_control
        try:
            return await self._attempt(
                model, system=system, user=user, schema=schema, role=role, timeout=timeout,
                max_output_tokens=max_output_tokens, disable_thinking=want_thinking_control,
            )
        except genai_errors.ClientError as exc:
            if exc.code == 400 and want_thinking_control:
                logger.info("%s rejected an explicit thinking budget; retrying without it", model)
                self._no_thinking_control.add(model)
                # Recurse rather than call _attempt directly: this re-enters the
                # try/except above so a failure on the retry (e.g. a 503 while
                # the model is overloaded) is still translated and handled by
                # generate()'s fallback chain instead of crashing the process.
                return await self._call_one(
                    model, system=system, user=user, schema=schema, role=role, timeout=timeout,
                    max_output_tokens=max_output_tokens,
                )
            raise self._translate(model, exc) from exc
        except genai_errors.APIError as exc:
            raise self._translate(model, exc) from exc
        except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as exc:
            raise LlmError(f"gemini {model} unreachable after {timeout:.1f}s: {exc}") from exc

    @staticmethod
    def _translate(model: str, exc: genai_errors.APIError) -> LlmError:
        """Map a provider error onto this module's taxonomy."""
        if exc.code == _RATE_LIMIT_STATUS:
            return LlmRateLimited(f"gemini {model}: quota exhausted (429)")
        if exc.code in _FATAL_STATUS:
            return LlmUnavailable(f"gemini {model}: {exc.code} {exc.message}")
        return LlmError(f"gemini {model}: {exc.code} {exc.message}")

    async def _attempt(
        self, model: str, *, system: str, user: str, schema: type[T], role: ModelRole,
        timeout: float, max_output_tokens: int, disable_thinking: bool,
    ) -> T:
        """One request; provider exceptions propagate for :meth:`_call_one` to classify."""
        config = genai_types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_output_tokens,
        )
        if disable_thinking:
            config.thinking_config = genai_types.ThinkingConfig(thinking_budget=0)

        started = time.monotonic()
        response = await asyncio.wait_for(
            self._client.aio.models.generate_content(model=model, contents=user, config=config),
            timeout=timeout,
        )

        parsed = response.parsed
        if parsed is None:
            finish = response.candidates[0].finish_reason if response.candidates else None
            raise LlmDeclined(f"gemini {model} returned no structured output (finish_reason={finish})")
        usage = response.usage_metadata
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "gemini %s %.0fms, %s in / %s out",
            model, elapsed_ms,
            usage.prompt_token_count if usage else "?", usage.candidates_token_count if usage else "?",
        )
        self._report_usage(
            model, role,
            usage.prompt_token_count if usage else 0,
            usage.candidates_token_count if usage else 0,
            elapsed_ms,
        )
        return parsed  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #

class AnthropicLlm(_ChainedLlm):
    """Anthropic Claude behind :class:`LlmClient`."""

    provider = "claude"

    def __init__(
        self,
        models: dict[ModelRole, tuple[str, ...]] | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        """``client`` is injectable for tests; ``api_key`` overrides ``ANTHROPIC_API_KEY``."""
        super().__init__(models or ANTHROPIC_MODELS)
        self._client = client or anthropic.AsyncAnthropic(
            timeout=ROLE_TIMEOUTS[ModelRole.SMART], max_retries=0, **({"api_key": api_key} if api_key else {})
        )

    async def _call_one(
        self, model: str, *, system: str, user: str, schema: type[T], role: ModelRole,
        timeout: float, max_output_tokens: int,
    ) -> T:
        """One structured-output request."""
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._client.messages.parse(
                    model=model,
                    max_tokens=max_output_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_format=schema,
                ),
                timeout=timeout,
            )
        except anthropic.AuthenticationError as exc:
            raise LlmUnavailable(f"claude {model}: ANTHROPIC_API_KEY rejected") from exc
        except anthropic.NotFoundError as exc:
            raise LlmUnavailable(f"claude {model}: model not found") from exc
        except anthropic.RateLimitError as exc:
            raise LlmRateLimited(f"claude {model}: quota exhausted (429)") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in _FATAL_STATUS:
                raise LlmUnavailable(f"claude {model}: {exc.status_code} {exc.message}") from exc
            raise LlmError(f"claude {model}: API error {exc.status_code}: {exc.message}") from exc
        except (anthropic.APIConnectionError, asyncio.TimeoutError) as exc:
            raise LlmError(f"claude {model} unreachable after {timeout:.1f}s: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = response.stop_details.explanation if response.stop_details else "no explanation"
            raise LlmDeclined(f"claude {model} refused: {detail}")
        if response.parsed_output is None:
            raise LlmDeclined(f"claude {model} returned no structured output (stop_reason={response.stop_reason})")
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "claude %s (%s) %.0fms, %d in / %d out",
            model, role.value, elapsed_ms, response.usage.input_tokens, response.usage.output_tokens,
        )
        self._report_usage(
            model, role, response.usage.input_tokens, response.usage.output_tokens, elapsed_ms
        )
        return response.parsed_output


# --------------------------------------------------------------------------- #
# Construction and startup checks
# --------------------------------------------------------------------------- #

def _models_from_env(provider: str, defaults: dict[ModelRole, tuple[str, ...]]) -> dict[ModelRole, tuple[str, ...]]:
    """Apply ``DELEG8_*_MODEL`` overrides, which put the named model at the head of its chain."""
    resolved = dict(defaults)
    for role in ModelRole:
        override = os.getenv(_ENV_OVERRIDES[(provider, role)], "").strip()
        if override:
            rest = tuple(m for m in defaults[role] if m != override)
            resolved[role] = (override, *rest)
    return resolved


def build_llm(*, anthropic_key: str | None = None, gemini_key: str | None = None) -> LlmClient | None:
    """Claude when an Anthropic key is available, else Gemini, else ``None``.

    Keys may be passed explicitly - that is how a tenant's *own* key is used
    without mutating ``os.environ``, which one process serving many users
    obviously cannot do - and fall back to the platform environment when not.

    Returns ``None`` only when neither key is present, which every caller must
    treat as "the delegate has no brain" rather than as a quiet degradation.
    """
    claude = (anthropic_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
    gemini = (gemini_key or os.getenv("GEMINI_API_KEY", "")).strip()
    if claude:
        client = AnthropicLlm(_models_from_env("anthropic", ANTHROPIC_MODELS), api_key=claude)
        logger.info("llm provider: %s", client.describe())
        return client
    if gemini:
        client = GeminiLlm(_models_from_env("gemini", GEMINI_MODELS), api_key=gemini)
        logger.info("llm provider: %s", client.describe())
        return client
    logger.warning("llm provider: none (no Anthropic or Gemini key)")
    return None


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of the startup liveness check."""

    ok: bool
    provider: str
    detail: str
    elapsed_ms: float

    def line(self) -> str:
        """One-line summary for the console."""
        mark = "ok" if self.ok else "FAILED"
        return f"[LLM] {self.provider}: {mark} ({self.elapsed_ms:.0f}ms) {self.detail}"


async def preflight(client: LlmClient | None) -> PreflightResult:
    """Check every configured model with one real call. Never raises."""
    started = time.monotonic()
    if client is None:
        return PreflightResult(
            ok=False, provider="none",
            detail="no ANTHROPIC_API_KEY or GEMINI_API_KEY; the delegate cannot think",
            elapsed_ms=0.0,
        )
    try:
        await client.preflight()
    except LlmError as exc:
        return PreflightResult(
            ok=False, provider=client.provider, detail=str(exc),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    return PreflightResult(
        ok=True, provider=client.provider, detail=client.describe(),
        elapsed_ms=(time.monotonic() - started) * 1000,
    )


async def preflight_or_raise(client: LlmClient | None) -> PreflightResult:
    """:func:`preflight`, but a failure is fatal.

    Used by the entry points that are about to join a real meeting. Refusing to
    join is the right behaviour: a delegate whose brain is dead will sit in the
    call quoting fragments of your PDFs at people, which is worse than not
    being there at all.

    Raises:
        LlmUnavailable: when the check did not pass.
    """
    result = await preflight(client)
    if not result.ok:
        raise LlmUnavailable(
            f"the language model is not usable: {result.detail}. "
            "Fix the key or the model id, or set DELEG8_ALLOW_NO_LLM=true to run without a brain."
        )
    return result


def allow_no_llm() -> bool:
    """True when ``DELEG8_ALLOW_NO_LLM`` permits joining a meeting with a dead brain."""
    return os.getenv("DELEG8_ALLOW_NO_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
