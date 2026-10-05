"""
Recording consent: announcing the delegate, and - where required - waiting for a yes.

The delegate already tells anyone who asks that it is an AI. That is a
different and much lower bar than consent to record. Several jurisdictions
(California, Florida, Illinois, Pennsylvania, Washington and much of the EU
among them) require *every* party to a call to agree before it may be
recorded or transcribed, and a system that transcribes first and mentions it
afterwards fails that test no matter how politely it self-discloses.

So consent is a gate, not a notice:

* **notify_only** - announce on joining, keep going. Appropriate where the
  principal's own consent is sufficient and the room has been told.
* **single_party** - identical behaviour, recorded under a different legal
  basis, so the audit trail says which one was claimed.
* **all_party** - announce, then retain *nothing* until the room agrees.
  While :attr:`ConsentState.PENDING`, utterances are classified for agreement
  or objection and then discarded: they never reach the policy engine, the
  meeting memory, the commitments ledger or the briefing. On agreement the
  gate opens; on refusal or timeout the delegate says it will not transcribe
  and leaves.

Every outcome is written to ``consent_records`` with what was announced, what
was said back, and who said it - because the evidence that nothing was
retained is itself the thing you need to be able to produce.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.db.models import ConsentMode, ConsentState

logger = logging.getLogger("deleg8.compliance.consent")

DEFAULT_TIMEOUT_SECONDS: float = 90.0
"""How long the room has to answer before an all-party meeting is abandoned."""


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #

_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|of course|certainly|go ahead|that'?s fine|fine by me|"
    r"no problem|no objection|i consent|we consent|i agree|we agree|agreed|ok|okay|"
    r"that works|please do|happy for|fine with me|all good|works for me)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|don'?t|do not|please don'?t|stop recording|not comfortable|i object|"
    r"we object|i refuse|without my consent|turn (it|that) off|no recording|"
    r"i'?d rather not|rather you didn'?t|not ok(ay)?|absolutely not)\b",
    re.IGNORECASE,
)
_WITHDRAWAL = re.compile(
    r"\b(stop recording|stop transcribing|delete (the|that) (recording|transcript)|"
    r"i withdraw|withdraw my consent|turn off the (recording|bot|transcription)|"
    r"please leave|leave the (call|meeting))\b",
    re.IGNORECASE,
)


class ConsentSignal(str, Enum):
    """What one utterance said about consent."""

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    WITHDRAWAL = "withdrawal"
    NEUTRAL = "neutral"


def classify(text: str) -> ConsentSignal:
    """Read one utterance as agreement, refusal, withdrawal, or neither.

    Deterministic on purpose. This runs on every utterance while the gate is
    pending, in a live call, and it decides whether anything at all may be
    retained - so it must be fast, auditable, and must not depend on a model
    that might be rate-limited at that exact moment. Ambiguity resolves to
    NEUTRAL, which keeps the gate shut rather than guessing it open.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ConsentSignal.NEUTRAL
    if _WITHDRAWAL.search(cleaned):
        return ConsentSignal.WITHDRAWAL
    # Refusal is checked before agreement: "yes, but please don't record" must
    # not read as a yes, and the cost of a false negative (asking again) is far
    # below the cost of a false positive (recording someone who said no).
    if _NEGATIVE.search(cleaned):
        return ConsentSignal.NEGATIVE
    if _AFFIRMATIVE.search(cleaned):
        return ConsentSignal.AFFIRMATIVE
    return ConsentSignal.NEUTRAL


def disclosure_line(principal: str, mode: ConsentMode, *, delegate_name: str = "DELEG8") -> str:
    """What the delegate says on joining."""
    base = (
        f"Hello - I'm {delegate_name}, an AI delegate attending on behalf of {principal}. "
        "I transcribe what is said so I can respond and write a summary for them. "
        "No audio or video is recorded."
    )
    if mode is ConsentMode.ALL_PARTY:
        return base + " Before I start, is everyone comfortable with that? Please say yes or no."
    return base


REFUSAL_LINE: str = (
    "Understood - I won't transcribe anything. I'm leaving the call now, and nothing "
    "from this conversation has been kept."
)

GRANTED_LINE: str = "Thank you. I'll take notes from here."

REMINDER_LINE: str = (
    "Sorry to interrupt - I still need a yes or no on transcribing this call before I can take part."
)

WITHDRAWN_LINE: str = (
    "Of course. I've stopped transcribing and I'm leaving the call. Nothing further will be kept."
)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

@dataclass
class ConsentDecision:
    """One resolution of the gate, ready to be written to the audit trail."""

    mode: ConsentMode
    outcome: ConsentState
    method: str
    granted_by: str | None = None
    evidence: str | None = None
    disclosure_text: str | None = None
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConsentGate:
    """Decides, for one meeting, whether anything said may be retained.

    The orchestrator asks :meth:`should_process` before doing anything with an
    utterance. While the gate is closed the answer is ``False`` and the
    utterance is fed to :meth:`observe` instead, which looks for a yes or a no
    and then throws the text away.
    """

    def __init__(
        self,
        *,
        mode: ConsentMode,
        principal: str,
        meeting_id: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        on_decision: Callable[[ConsentDecision], None] | None = None,
        delegate_name: str = "DELEG8",
    ) -> None:
        """Open a gate in ``mode``. ``on_decision`` receives every resolution for the audit trail."""
        self.mode = mode
        self.principal = principal
        self.meeting_id = meeting_id
        self.timeout_seconds = timeout_seconds
        self.delegate_name = delegate_name
        self._on_decision = on_decision
        self.state: ConsentState = (
            ConsentState.PENDING if mode is ConsentMode.ALL_PARTY else ConsentState.NOT_REQUIRED
        )
        self.disclosure: str = disclosure_line(principal, mode, delegate_name=delegate_name)
        self.announced_at: datetime | None = None
        self.decided_at: datetime | None = None
        self.granted_by: str | None = None
        self.evidence: str | None = None
        self.reminders_sent: int = 0
        self.discarded_utterances: int = 0
        """How many utterances were heard and thrown away while the gate was shut."""

    # -- state ------------------------------------------------------------ #

    @property
    def may_retain(self) -> bool:
        """True when the transcript may reach memory, the archive and the briefing."""
        return self.state.may_retain

    @property
    def is_pending(self) -> bool:
        """True while waiting for the room to answer."""
        return self.state is ConsentState.PENDING

    @property
    def is_refused(self) -> bool:
        """True when consent was refused, timed out, or withdrawn."""
        return self.state in {ConsentState.DENIED, ConsentState.TIMED_OUT, ConsentState.WITHDRAWN}

    def should_process(self) -> bool:
        """Whether an incoming utterance may go through the normal turn pipeline."""
        return self.may_retain

    def describe(self) -> str:
        """One line for logs and the console."""
        label = {
            ConsentState.NOT_REQUIRED: "announced (no wait required)",
            ConsentState.PENDING: "waiting for the room to agree",
            ConsentState.GRANTED: "granted",
            ConsentState.DENIED: "refused",
            ConsentState.TIMED_OUT: "no answer within the window",
            ConsentState.WITHDRAWN: "withdrawn during the call",
        }[self.state]
        return f"consent ({self.mode.value}): {label}"

    def announce(self) -> str:
        """Mark the disclosure as delivered and return it."""
        self.announced_at = datetime.now(timezone.utc)
        return self.disclosure

    def elapsed_seconds(self) -> float:
        """How long the gate has been waiting."""
        if self.announced_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.announced_at).total_seconds()

    def timed_out(self) -> bool:
        """True when the room never answered within the window."""
        return self.is_pending and self.elapsed_seconds() > self.timeout_seconds

    # -- transitions ------------------------------------------------------ #

    def _settle(
        self, outcome: ConsentState, *, method: str, by: str | None, evidence: str | None
    ) -> ConsentDecision:
        self.state = outcome
        self.decided_at = datetime.now(timezone.utc)
        self.granted_by = by
        self.evidence = evidence
        decision = ConsentDecision(
            mode=self.mode,
            outcome=outcome,
            method=method,
            granted_by=by,
            evidence=evidence,
            disclosure_text=self.disclosure,
            decided_at=self.decided_at,
        )
        logger.info("consent for %s: %s via %s", self.meeting_id, outcome.value, method)
        if self._on_decision is not None:
            try:
                self._on_decision(decision)
            except Exception:  # noqa: BLE001 - the audit write must not break the meeting
                logger.exception("could not record the consent decision for %s", self.meeting_id)
        return decision

    def observe(self, text: str, speaker: str | None = None) -> tuple[ConsentSignal, str | None]:
        """Feed one utterance to the gate. Returns ``(signal, line_to_say_or_None)``.

        Called for every utterance: while pending it is looking for a yes or no,
        and once open it is still watching for someone withdrawing consent
        mid-call, which is a right people actually have.
        """
        signal = classify(text)
        if self.is_pending:
            self.discarded_utterances += 1
            if signal is ConsentSignal.AFFIRMATIVE:
                self._settle(ConsentState.GRANTED, method="verbal", by=speaker, evidence=text)
                return signal, GRANTED_LINE
            if signal in (ConsentSignal.NEGATIVE, ConsentSignal.WITHDRAWAL):
                self._settle(ConsentState.DENIED, method="verbal", by=speaker, evidence=text)
                return signal, REFUSAL_LINE
            if self.timed_out():
                self._settle(ConsentState.TIMED_OUT, method="timeout", by=None, evidence=None)
                return signal, REFUSAL_LINE
            # Someone is talking but not answering; nudge, at most twice.
            if self.reminders_sent < 2 and self.discarded_utterances % 3 == 0:
                self.reminders_sent += 1
                return signal, REMINDER_LINE
            return signal, None

        if self.may_retain and signal is ConsentSignal.WITHDRAWAL:
            self._settle(ConsentState.WITHDRAWN, method="verbal", by=speaker, evidence=text)
            return signal, WITHDRAWN_LINE
        return signal, None

    def grant_externally(self, *, by: str, method: str = "principal") -> ConsentDecision:
        """The principal granted consent from the console or Telegram."""
        return self._settle(ConsentState.GRANTED, method=method, by=by, evidence=None)

    def deny_externally(self, *, by: str, method: str = "principal") -> ConsentDecision:
        """The principal refused consent from the console or Telegram."""
        return self._settle(ConsentState.DENIED, method=method, by=by, evidence=None)

    def expire(self) -> ConsentDecision | None:
        """Settle a pending gate that has run out of time. Returns the decision, or ``None``."""
        if self.is_pending and self.timed_out():
            return self._settle(ConsentState.TIMED_OUT, method="timeout", by=None, evidence=None)
        return None

    def record_opening(self) -> ConsentDecision:
        """Write the announcement itself to the audit trail for non-blocking modes."""
        return ConsentDecision(
            mode=self.mode,
            outcome=self.state,
            method="announcement",
            granted_by=None,
            evidence=None,
            disclosure_text=self.disclosure,
        )


def consent_timeout_from_env() -> float:
    """``DELEG8_CONSENT_TIMEOUT_SECONDS`` (default 90) - how long the room has to answer."""
    raw = os.getenv("DELEG8_CONSENT_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(15.0, value)


def mode_from_env(default: ConsentMode = ConsentMode.NOTIFY_ONLY) -> ConsentMode:
    """``DELEG8_CONSENT_MODE`` - how a worker container learns which mode to run in."""
    return mode_from_value(os.getenv("DELEG8_CONSENT_MODE", "").strip() or None, default)


def mode_from_value(value: str | None, default: ConsentMode = ConsentMode.NOTIFY_ONLY) -> ConsentMode:
    """Parse a stored or submitted consent mode, falling back to ``default``."""
    try:
        return ConsentMode(str(value))
    except (ValueError, TypeError):
        return default


def describe_modes() -> list[dict[str, str]]:
    """The consent modes, for the console's picker."""
    return [
        {
            "key": ConsentMode.NOTIFY_ONLY.value,
            "name": "Announce only",
            "summary": (
                "The delegate announces itself and that it transcribes, then takes part. "
                "Use where your own consent is the legal basis and the room has been told."
            ),
        },
        {
            "key": ConsentMode.ALL_PARTY.value,
            "name": "Wait for everyone to agree",
            "summary": (
                "Nothing is kept until someone in the room says yes. If anyone refuses, or "
                "nobody answers, the delegate says so and leaves. Required in all-party consent "
                "jurisdictions such as California, Illinois and much of the EU."
            ),
        },
        {
            "key": ConsentMode.SINGLE_PARTY.value,
            "name": "Single-party consent",
            "summary": (
                "Same behaviour as announce-only, recorded against a single-party legal basis. "
                "Only choose this if you are sure it applies where every participant is."
            ),
        },
    ]
