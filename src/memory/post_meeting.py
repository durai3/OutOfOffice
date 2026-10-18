"""
Post-meeting engine: turn the recorded meeting state into an executive
briefing and push it to the principal.

    MeetingState.conclude()  ->  MeetingSnapshot        CommitmentLedger
                                     |                        |
                       Summariser.summarise()                 |
                        (prose: summary, decisions,           |
                         unresolved questions)                |
                                     |                        |
                              ExecutiveBriefing <-------------+
                                     |                  (action items)
                render_briefing_markdown_v2() / render_briefing_plain()
                                     |
                       MessageDispatcher.send_text()   (the Telegram bridge)

Two summarisers ship:

* :class:`LlmSummariser`      - asks whichever model :mod:`src.llm` selected for
  a structured :class:`BriefingDraft`. One implementation for every provider;
  the previous pair of provider-specific classes each hardcoded a model id, and
  one of those ids had been retired, so every briefing silently came from the
  template with the reason buried in an archived JSON file.
* :class:`TemplateSummariser` - deterministic sentences built from the snapshot.
  Used without a model, and as the fallback when the call fails, so a briefing
  is always delivered. A fallback is now also reported to :mod:`src.health`, so
  the console says the briefing was degraded rather than hiding it.

Action items never come from the summariser. They come from the
:class:`~src.memory.commitments.CommitmentLedger`, which assembled them live and
knows which were independently confirmed, and they are additionally sent as
their own Telegram message so a long meeting cannot truncate them away.

Triggers: the ``/conclude`` Telegram command, ``POST /api/v1/meetings/{id}/conclude``,
or the end of a voice session; all of them call :meth:`PostMeetingReporter.conclude`.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.brain.profile_store import ProfileNotFoundError, ProfileStore
from src.escalation.bridge import MessageDispatcher
from src.escalation.models import DeliveryReceipt
from src.health import COMPONENT_BRIEFING, report_degraded, report_healthy
from src.integrations.base import BriefingContent
from src.integrations.dispatcher import dispatch_briefing
from src.llm import LlmClient, LlmError, ModelRole, build_llm
from src.memory.commitments import (
    Commitment,
    CommitmentRegistry,
    CommitmentStatus,
    due_soon,
)
from src.memory.meeting_state import (
    DecisionOrigin,
    MeetingSnapshot,
    MeetingState,
    MeetingStateRegistry,
)

logger = logging.getLogger("deleg8.memory.post_meeting")

_MAX_TRANSCRIPT_LINES: int = 400
_MAX_BRIEFING_CHARS: int = 3900
"""Keep rendered briefings under Telegram's 4096-character limit with headroom."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #

class ActionItemSummary(BaseModel):
    """One action item as it appears in the briefing."""

    description: str = Field(..., description="Imperative verb phrase, e.g. 'Finish the security review'.")
    owner: str = Field(..., description="Owner as named in the record; 'Unassigned' when nobody owns it.")
    due: str = Field(default="", description="Resolved date or the phrase spoken; empty string when none.")


class ExecutiveBriefing(BaseModel):
    """The structured post-meeting summary."""

    executive_summary: str = Field(..., description="Two to three sentences written for a busy executive.")
    decisions_made: list[str] = Field(default_factory=list, description="One short line per agreed decision.")
    action_items: list[ActionItemSummary] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(
        default_factory=list, description="Questions escalated, deferred, or left unanswered."
    )


class BriefingDraft(BaseModel):
    """The part of the briefing a model is asked to write. Flat, and prose only.

    Action items are deliberately absent. They come from
    :class:`~src.memory.commitments.CommitmentLedger`, which assembled them from
    the conversation as it happened and knows whether each was independently
    confirmed - asking a model to re-derive them from the transcript would throw
    that away and re-introduce exactly the guessing the ledger exists to avoid.

    Flat (no arrays of objects) because ``gemini-3.6-flash`` rejects nested
    response schemas and reports it as a 503.
    """

    executive_summary: str = Field(..., description="Two to three sentences written for a busy executive.")
    decisions_made: list[str] = Field(default_factory=list, description="One short line per agreed decision.")
    unresolved_questions: list[str] = Field(
        default_factory=list, description="Questions escalated, deferred, or left unanswered, with who asked."
    )


class PostMeetingReport(BaseModel):
    """Everything that happened when a meeting was concluded."""

    meeting_id: str
    concluded_at: datetime
    briefing: ExecutiveBriefing
    summariser: str = Field(..., description="Which summariser produced the briefing.")
    fallback_reason: str | None = Field(default=None, description="Why the primary summariser was not used.")
    text: str = Field(..., description="The briefing as plain text.")
    receipt: DeliveryReceipt
    snapshot: MeetingSnapshot


class SummaryError(RuntimeError):
    """Raised by a summariser when it cannot produce a briefing."""


@runtime_checkable
class ReportArchive(Protocol):
    """Somewhere concluded meetings are kept (see :mod:`src.memory.history_store`)."""

    def save(self, report: PostMeetingReport) -> str:
        """Persist ``report``; returns an identifier for it."""
        ...


@runtime_checkable
class Summariser(Protocol):
    """Produces an :class:`ExecutiveBriefing` from a meeting snapshot."""

    def describe(self) -> str:
        """One-line label used in the briefing footer and health checks."""
        ...

    async def summarise(self, snapshot: MeetingSnapshot, *, principal: str) -> ExecutiveBriefing:
        """Build the briefing. Raises :class:`SummaryError` on failure."""
        ...


# --------------------------------------------------------------------------- #
# Template summariser (no model, always available)
# --------------------------------------------------------------------------- #

def _humanise_topic(topic: str) -> str:
    return topic.replace("_", " ")


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _topics_discussed(snapshot: MeetingSnapshot) -> list[str]:
    seen: list[str] = []
    for turn in snapshot.transcript:
        if turn.is_delegate or not turn.topic or turn.topic == "documented_facts":
            continue
        label = _humanise_topic(turn.topic)
        if label not in seen:
            seen.append(label)
    if snapshot.active_topic and snapshot.active_topic.lower() not in {s.lower() for s in seen}:
        seen.insert(0, snapshot.active_topic)
    return seen


def build_template_briefing(snapshot: MeetingSnapshot, *, principal: str) -> ExecutiveBriefing:
    """Deterministic briefing assembled from the recorded state alone."""
    stats = snapshot.stats
    others = [p for p in snapshot.participants if p != principal]
    topics = _topics_discussed(snapshot)
    duration = snapshot.duration_minutes

    if stats.utterances == 0:
        opening = f"No utterances were recorded for meeting {snapshot.meeting_id}."
    else:
        who = f" with {_join(others)}" if others else ""
        about = f", centred on {_join(topics)}" if topics else ""
        length = f"{duration:.0f} minute" if duration >= 1 else "under a minute"
        opening = (
            f"Meeting {snapshot.meeting_id} ran for {length}{'s' if duration >= 2 else ''}{who} "
            f"across {stats.utterances} utterance{'s' if stats.utterances != 1 else ''}{about}."
        )
    movement = (
        f"The delegate answered {stats.auto_replies} question{'s' if stats.auto_replies != 1 else ''} from the "
        f"knowledge base and escalated {stats.escalations}; {len(snapshot.agreed_decisions)} "
        f"decision{'s were' if len(snapshot.agreed_decisions) != 1 else ' was'} recorded and "
        f"{len(snapshot.pending_actions)} action item{'s' if len(snapshot.pending_actions) != 1 else ''} assigned."
    )
    if snapshot.open_questions:
        first = snapshot.open_questions[0]
        closing = (
            f"{len(snapshot.open_questions)} question{'s' if len(snapshot.open_questions) != 1 else ''} still "
            f"need{'s' if len(snapshot.open_questions) == 1 else ''} {principal}'s attention, starting with "
            f"{first.asked_by}'s: \"{first.text}\""
        )
    else:
        closing = f"Nothing is waiting on {principal}."

    decisions = [
        d.text if d.origin is DecisionOrigin.UTTERANCE else f"{d.text} (via Telegram)"
        for d in snapshot.agreed_decisions
    ]
    actions = [
        ActionItemSummary(
            description=a.description,
            owner=f"{a.owner} (inferred)" if a.owner_inferred else a.owner,
            due=a.due_label or "",
        )
        for a in snapshot.pending_actions
    ]
    questions = [f"{q.text} ({q.asked_by}; {q.outcome})" for q in snapshot.open_questions]
    return ExecutiveBriefing(
        executive_summary=" ".join((opening, movement, closing)),
        decisions_made=decisions,
        action_items=actions,
        unresolved_questions=questions,
    )


class TemplateSummariser:
    """Deterministic summariser; never fails, never invents."""

    def describe(self) -> str:
        """Label."""
        return "template (deterministic, from meeting state)"

    async def summarise(self, snapshot: MeetingSnapshot, *, principal: str) -> ExecutiveBriefing:
        """Build the briefing from the snapshot."""
        return build_template_briefing(snapshot, principal=principal)


# --------------------------------------------------------------------------- #
# Claude summariser
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT: str = """\
You are the post-meeting analyst for DELEG8, an AI delegate that attended a meeting on behalf of {principal}.
You receive the structured record the delegate kept during the meeting (transcript, decisions it detected,
action items, questions it escalated to {principal}, and counters) and you write the executive briefing
{principal} reads afterwards.

Ground rules:
- Use only what is in the record. Never invent decisions, owners, dates, numbers or commitments.
  If the record has no decisions, return an empty list; do the same for actions and questions.
- executive_summary: two or three plain sentences for a busy executive - what the meeting was about,
  what moved, and what needs {principal}. No bullet points, no headings.
- decisions_made: one short line each, in the past tense. Include agreements people voiced in the room
  and anything marked as approved or declined by {principal} through the escalation channel.
- action_items: description as an imperative verb phrase; owner exactly as named in the record
  ("Unassigned" if none); due as the resolved date when the record has one, otherwise the phrase spoken,
  otherwise an empty string.
- unresolved_questions: every question the delegate escalated, deferred, could not answer, or that timed out,
  quoted closely enough that {principal} recognises it, with who asked it.
"""


def snapshot_for_llm(snapshot: MeetingSnapshot) -> dict[str, Any]:
    """Compact, JSON-serialisable view of the snapshot for the model."""
    transcript = snapshot.transcript[-_MAX_TRANSCRIPT_LINES:]
    return {
        "meeting_id": snapshot.meeting_id,
        "started_at": snapshot.started_at.isoformat(),
        "concluded_at": snapshot.concluded_at.isoformat() if snapshot.concluded_at else None,
        "duration_minutes": round(snapshot.duration_minutes, 1),
        "participants": snapshot.participants,
        "active_topic_at_end": snapshot.active_topic,
        "stats": snapshot.stats.model_dump(),
        "transcript": [
            {
                "speaker": t.speaker,
                "text": t.text,
                "delegate_decision": t.decision.value if t.decision else None,
                "policy_topic": t.topic,
            }
            for t in transcript
        ],
        "detected_decisions": [
            {"text": d.text, "proposed_by": d.proposed_by, "origin": d.origin.value} for d in snapshot.agreed_decisions
        ],
        "detected_action_items": [
            {
                "description": a.description,
                "owner": a.owner,
                "owner_inferred": a.owner_inferred,
                "due_phrase": a.due_text,
                "due_date": a.due_date.isoformat() if a.due_date else None,
            }
            for a in snapshot.pending_actions
        ],
        "escalated_or_open_questions": [
            {"text": q.text, "asked_by": q.asked_by, "policy_topic": q.topic, "tier": q.tier.value if q.tier else None, "outcome": q.outcome}
            for q in snapshot.open_questions
        ],
    }


class LlmSummariser:
    """Writes the briefing prose with whichever model :mod:`src.llm` selected.

    Replaces the previous pair of provider-specific summarisers. They each
    hardcoded their own model id, and one of those ids
    (``gemini-2.5-flash``) had been retired - so every briefing silently came
    from the template instead, with the reason buried in an archived JSON file
    nobody reads.
    """

    def __init__(self, llm: LlmClient) -> None:
        """Bind to a configured language model."""
        self._llm = llm

    def describe(self) -> str:
        """Label."""
        return self._llm.describe()

    async def summarise(self, snapshot: MeetingSnapshot, *, principal: str) -> ExecutiveBriefing:
        """One structured-output request. Raises :class:`SummaryError` on any failure."""
        record = json.dumps(snapshot_for_llm(snapshot), ensure_ascii=False, indent=2)
        try:
            draft = await self._llm.generate(
                system=_SYSTEM_PROMPT.format(principal=principal),
                user="Here is the meeting record. Produce the executive briefing.\n\n" + record,
                schema=BriefingDraft,
                role=ModelRole.SMART,
                max_output_tokens=4096,
            )
        except LlmError as exc:
            raise SummaryError(str(exc)) from exc
        return ExecutiveBriefing(
            executive_summary=draft.executive_summary,
            decisions_made=draft.decisions_made,
            action_items=[],  # filled from the commitment ledger by the reporter
            unresolved_questions=draft.unresolved_questions,
        )


def build_summariser(llm: LlmClient | None = None) -> Summariser:
    """An LLM summariser when a model is configured, else the deterministic template."""
    client = llm if llm is not None else build_llm()
    if client is None:
        logger.info("post-meeting summariser: template (no language model configured)")
        return TemplateSummariser()
    logger.info("post-meeting summariser: %s", client.describe())
    return LlmSummariser(client)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_MD2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _md(text: str) -> str:
    """Escape arbitrary text for Telegram MarkdownV2."""
    return _MD2_SPECIAL.sub(r"\\\1", text)


def _header_line(snapshot: MeetingSnapshot) -> str:
    when = (snapshot.concluded_at or _utc_now()).astimezone().strftime("%a %d %b %Y %H:%M")
    people = ", ".join(snapshot.participants) if snapshot.participants else "no participants recorded"
    return f"{when} · {snapshot.duration_minutes:.0f} min · {snapshot.stats.utterances} utterances · {people}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_briefing_plain(briefing: ExecutiveBriefing, snapshot: MeetingSnapshot, *, summariser: str) -> str:
    """Plain-text briefing (logs, API responses, Telegram fallback)."""
    lines = [
        f"EXECUTIVE BRIEFING: {snapshot.meeting_id}",
        _header_line(snapshot),
        f"Summary by {summariser}",
        "",
        "Executive Summary",
        briefing.executive_summary.strip(),
        "",
        "Decisions Made",
        *([f"- {d}" for d in briefing.decisions_made] or ["- None recorded"]),
        "",
        "Action Items & Owners",
        *(
            [f"- {a.description} | owner: {a.owner}" + (f" | due: {a.due}" if a.due else "") for a in briefing.action_items]
            or ["- None recorded"]
        ),
        "",
        "Unresolved Questions",
        *([f"- {q}" for q in briefing.unresolved_questions] or ["- None"]),
    ]
    return _clip("\n".join(lines), _MAX_BRIEFING_CHARS)


def render_briefing_markdown_v2(briefing: ExecutiveBriefing, snapshot: MeetingSnapshot, *, summariser: str) -> str:
    """Telegram MarkdownV2 briefing with bold section headers."""
    decisions = [f"• {_md(d)}" for d in briefing.decisions_made] or ["_None recorded_"]
    actions = [
        f"• {_md(a.description)} — *{_md(a.owner)}*" + (f" · due {_md(a.due)}" if a.due else "")
        for a in briefing.action_items
    ] or ["_None recorded_"]
    questions = [f"• {_md(q)}" for q in briefing.unresolved_questions] or ["_None_"]
    lines = [
        f"\U0001f4cb *Executive Briefing: {_md(snapshot.meeting_id)}*",
        f"_{_md(_header_line(snapshot))}_",
        f"_Summary by {_md(summariser)}_",
        "",
        "*Executive Summary*",
        _md(briefing.executive_summary.strip()),
        "",
        "*Decisions Made*",
        *decisions,
        "",
        "*Action Items & Owners*",
        *actions,
        "",
        "*Unresolved Questions*",
        *questions,
    ]
    text = "\n".join(lines)
    if len(text) > _MAX_BRIEFING_CHARS:
        # Trim the longest lists first so the summary always survives.
        text = "\n".join(lines[:9] + decisions[:5] + lines[9 + len(decisions):])
        text = _clip(text, _MAX_BRIEFING_CHARS)
    return text


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #

class PostMeetingReporter:
    """Concludes meetings: summarise, render, dispatch, and forget."""

    def __init__(
        self,
        dispatcher: MessageDispatcher,
        summariser: Summariser | None = None,
        *,
        fallback: Summariser | None = None,
        store: ProfileStore | None = None,
        archive: ReportArchive | None = None,
        llm: LlmClient | None = None,
        commitments: CommitmentRegistry | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """``summariser`` defaults to :func:`build_summariser`; ``fallback`` to the template.

        ``archive`` receives every report so the meeting history survives the process.
        ``commitments`` supplies the action items, which are taken from the ledger
        rather than re-derived from the transcript by a model. ``user_id`` is the
        account id used to look up enabled integrations to fan the briefing out to;
        left ``None`` (the command-line and worker paths), no integration is ever
        dispatched.
        """
        self._dispatcher = dispatcher
        self._archive = archive
        self._summariser: Summariser = summariser if summariser is not None else build_summariser(llm)
        self._fallback: Summariser = fallback if fallback is not None else TemplateSummariser()
        self._store = store
        self._commitments = commitments
        self._user_id = user_id

    @property
    def summariser(self) -> Summariser:
        """The primary summariser."""
        return self._summariser

    @property
    def bridge(self) -> MessageDispatcher:
        """Where briefings are delivered."""
        return self._dispatcher

    @bridge.setter
    def bridge(self, dispatcher: MessageDispatcher) -> None:
        """Swap the delivery channel in place (the tenant re-linked their phone)."""
        self._dispatcher = dispatcher

    def rebuild_summariser(self, llm: LlmClient | None) -> None:
        """Rebuild the summariser after the tenant's model credentials changed."""
        self._summariser = build_summariser(llm)

    def describe(self) -> str:
        """Status line for health checks."""
        return f"{self._summariser.describe()} -> {self._dispatcher.describe()}"

    @property
    def archive(self) -> ReportArchive | None:
        """Where concluded reports are kept, if anywhere."""
        return self._archive

    def _principal_name(self, user_id: str) -> str:
        if self._store is None:
            return user_id
        try:
            return self._store.get_profile(user_id).display_name
        except ProfileNotFoundError:
            return user_id

    def commitments_for(self, meeting_id: str) -> list[Commitment]:
        """Open commitments recorded for ``meeting_id``, if a ledger is wired in."""
        if self._commitments is None:
            return []
        ledger = self._commitments.get(meeting_id)
        return ledger.open_items() if ledger is not None else []

    @staticmethod
    def _action_items(commitments: list[Commitment], snapshot: MeetingSnapshot) -> list[ActionItemSummary]:
        """Action items for the briefing: the ledger when it has any, else the legacy extraction."""
        if commitments:
            return [
                ActionItemSummary(
                    description=c.description if c.status is CommitmentStatus.CONFIRMED else f"{c.description} (unconfirmed)",
                    owner=c.owner,
                    due=c.due_label,
                )
                for c in commitments
            ]
        return [
            ActionItemSummary(
                description=a.description,
                owner=f"{a.owner} (inferred)" if a.owner_inferred else a.owner,
                due=a.due_label or "",
            )
            for a in snapshot.pending_actions
        ]

    async def build_briefing(self, snapshot: MeetingSnapshot) -> tuple[ExecutiveBriefing, str, str | None]:
        """Summarise with the primary summariser, falling back on failure.

        Returns ``(briefing, summariser label, fallback reason or None)``. A
        fallback is also reported to :mod:`src.health`, so the console shows that
        the briefing was degraded instead of leaving it discoverable only by
        opening the archived JSON afterwards.
        """
        principal = self._principal_name(snapshot.user_id)
        commitments = self.commitments_for(snapshot.meeting_id)
        try:
            briefing = await self._summariser.summarise(snapshot, principal=principal)
            label, reason = self._summariser.describe(), None
            report_healthy(COMPONENT_BRIEFING)
        except SummaryError as exc:
            logger.warning("primary summariser failed (%s); using %s", exc, self._fallback.describe())
            report_degraded(COMPONENT_BRIEFING, "the briefing was written by the template, not a model", str(exc))
            briefing = await self._fallback.summarise(snapshot, principal=principal)
            label, reason = f"{self._fallback.describe()}; fallback", str(exc)

        briefing = briefing.model_copy(update={"action_items": self._action_items(commitments, snapshot)})
        return briefing, label, reason

    async def conclude(self, state: MeetingState, *, registry: MeetingStateRegistry | None = None) -> PostMeetingReport:
        """Close ``state``, produce the briefing, push it, and drop the meeting from ``registry``."""
        snapshot = state.conclude()
        briefing, label, fallback_reason = await self.build_briefing(snapshot)
        plain = render_briefing_plain(briefing, snapshot, summariser=label)
        formatted = render_briefing_markdown_v2(briefing, snapshot, summariser=label)
        receipt = await self._dispatcher.send_text(formatted, markdown_v2=True, plain_fallback=plain)
        await self._send_commitments(snapshot)
        if registry is not None:
            registry.remove(state.meeting_id)
        if self._commitments is not None:
            self._commitments.remove(state.meeting_id)
        logger.info(
            "meeting %s briefing by %s: delivered=%s via %s%s",
            state.meeting_id, label, receipt.delivered, receipt.channel,
            f" ({receipt.detail})" if receipt.detail else "",
        )
        report = PostMeetingReport(
            meeting_id=state.meeting_id,
            concluded_at=snapshot.concluded_at or _utc_now(),
            briefing=briefing,
            summariser=label,
            fallback_reason=fallback_reason,
            text=plain,
            receipt=receipt,
            snapshot=snapshot,
        )
        if self._archive is not None:
            try:
                self._archive.save(report)
            except Exception:  # noqa: BLE001 - archiving must never lose the briefing itself
                logger.exception("could not archive the briefing for %s", state.meeting_id)
        if self._user_id is not None:
            try:
                await dispatch_briefing(self._user_id, self._integration_content(state.meeting_id, briefing))
            except Exception:  # noqa: BLE001 - a connector failing must never fail the conclude itself
                logger.exception("integration dispatch failed for meeting %s", state.meeting_id)
        return report

    @staticmethod
    def _integration_content(meeting_id: str, briefing: ExecutiveBriefing) -> BriefingContent:
        """Project the briefing onto the connector framework's flat shape."""
        return BriefingContent(
            meeting_key=meeting_id,
            summary=briefing.executive_summary,
            decisions=list(briefing.decisions_made),
            action_items=[
                {"description": item.description, "owner": item.owner, "due": item.due}
                for item in briefing.action_items
            ],
            risks=list(briefing.unresolved_questions),
        )

    async def _send_commitments(self, snapshot: MeetingSnapshot) -> None:
        """Send the action items as their own message, ahead of any truncation.

        The briefing is clipped to fit Telegram's message limit, and the action
        items sit at the bottom of it - which is exactly the content the
        principal most needs and the first thing a long meeting would cut. A
        request someone made out loud reaching nobody is the failure this whole
        ledger exists to prevent, so it gets a message of its own.
        """
        commitments = self.commitments_for(snapshot.meeting_id)
        if not commitments:
            return
        urgent = [c for c in commitments if due_soon(c)]
        header = (
            f"⏰ *{len(commitments)} action item{'s' if len(commitments) != 1 else ''} "
            f"from {_md(snapshot.meeting_id)}*"
        )
        lines = [header, ""]
        for commitment in commitments:
            mark = "\U0001f534" if commitment in urgent else "•"
            detail = f"{mark} {_md(commitment.description)}"
            if commitment.due_label:
                detail += f"\n    _due {_md(commitment.due_label)}_"
            if commitment.requested_by:
                detail += f"\n    _asked by {_md(commitment.requested_by)}_"
            if commitment.status is not CommitmentStatus.CONFIRMED:
                detail += "\n    _heard once; not confirmed_"
            lines.append(detail)
        plain = "\n".join(line.replace("\\", "") for line in lines)
        receipt = await self._dispatcher.send_text("\n".join(lines), markdown_v2=True, plain_fallback=plain)
        if self._commitments is not None and receipt.delivered:
            ledger = self._commitments.get(snapshot.meeting_id)
            if ledger is not None:
                for commitment in commitments:
                    ledger.mark_notified(commitment.id)
        logger.info(
            "meeting %s: %d action item(s) sent, %d due today or tomorrow, delivered=%s",
            snapshot.meeting_id, len(commitments), len(urgent), receipt.delivered,
        )

    async def conclude_all(self, registry: MeetingStateRegistry) -> list[PostMeetingReport]:
        """Conclude every active meeting in ``registry`` (used by the ``/conclude`` command)."""
        reports: list[PostMeetingReport] = []
        for state in registry.active():
            reports.append(await self.conclude(state, registry=registry))
        return reports
