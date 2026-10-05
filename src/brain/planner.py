"""
The response planner: what the delegate actually says.

The original brain was a permission gate. Every path through it led to SILENT
unless a document lookup happened to clear a similarity threshold, so there was
no route to speech that was not a RAG hit - which is why, asked its own name,
the delegate said nothing at all, and why a person on the call had to repeat an
action item four times into silence.

This module inverts that. A single model call, given the principal's dossier,
the live conversation and the Representation Policy, decides what kind of turn
this is and writes the reply. The policy engine keeps its job, but that job is
now narrower and sharper: it decides what is *restricted*
(:class:`~src.brain.policy_engine.SensitivityCheck`), not whether speech is
permitted at all.

Two tracks, which is the whole safety design:

* **Conversational content** - greetings, meta questions about the delegate,
  acknowledgements, "I don't know" - is generated freely. Nothing about it can
  be wrong about the principal, because it makes no claim about them.
* **Personal and project facts** - GPA, employer, what they applied for, what a
  project uses - must be quoted from the dossier or the retrieved documents.
  The model marks these turns itself (``states_personal_fact``) and supplies
  verbatim quotes, which :class:`OutputGuard` checks programmatically before a
  word is spoken. A claim that does not verify never reaches the room; it is
  replaced by an honest deferral and logged as an open question.

That split is what lets the delegate be fluent without being able to invent a
GPA. It is also why the guard runs on the *output*: a sensitive subject can
surface in an answer even when the question that prompted it looked harmless.

Every schema here is flat (no arrays of objects). ``gemini-3.6-flash`` rejects
nested response schemas and reports it as a 503, which is indistinguishable
from a real outage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field

from src.brain.models import MeetingContext, PolicyRule, PolicyTier, UserProfile
from src.knowledge.grounding import verify_citations
from src.llm import LlmClient, LlmError, ModelRole

logger = logging.getLogger("deleg8.brain.planner")


class TurnType(str, Enum):
    """What kind of turn the planner decided this is."""

    SOCIAL = "social"
    """Greeting, small talk, or a meta question about the delegate itself.
    Free generation; makes no factual claim about the principal."""

    FACTUAL = "factual"
    """States something about the principal, their work, or their documents.
    Must be grounded and is checked by :class:`OutputGuard`."""

    COMMITMENT = "commitment"
    """Someone asked the principal to do something, or the delegate is
    acknowledging that it will pass a request on."""

    DEFLECT = "deflect"
    """A restricted subject. The delegate declines gracefully and a human is told."""

    UNKNOWN = "unknown"
    """A fair question the documents do not answer. The delegate says so and
    promises to come back - never guesses."""

    LEAVE = "leave"
    """Someone has asked the delegate to leave or end the call. The reply asks
    for one plain confirmation; the delegate does not actually depart until
    :class:`~src.turn_processor.TurnProcessor` sees that confirmation come back -
    leaving a live call is disruptive and hard to undo, so it is never done on
    a single, possibly mis-heard, utterance."""

    SILENT = "silent"
    """Nothing to say: the floor belongs to someone else, or the utterance
    wanted no response."""


_SPEAKING_TYPES: frozenset[TurnType] = frozenset(
    {TurnType.SOCIAL, TurnType.FACTUAL, TurnType.COMMITMENT, TurnType.DEFLECT, TurnType.UNKNOWN, TurnType.LEAVE}
)


class PlannedReply(BaseModel):
    """The planner's structured verdict for one turn. Flat by design."""

    turn_type: TurnType = Field(..., description="What kind of turn this is.")
    reply: str = Field(default="", description="Exactly what to say aloud. Empty when turn_type is silent.")
    states_personal_fact: bool = Field(
        default=False,
        description=(
            "True when the reply asserts anything about the principal, their history, "
            "documents, or projects. False for greetings, meta answers about being a "
            "delegate, acknowledgements, and deferrals."
        ),
    )
    supporting_quotes: list[str] = Field(
        default_factory=list,
        description=(
            "When states_personal_fact is true: exact, verbatim sentences or bullets "
            "copied character-for-character from the dossier or documents supplied, "
            "evidencing every fact asserted. Never paraphrase - an automated check "
            "rejects the reply if a quote does not appear verbatim in the source."
        ),
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence the reply is correct and appropriate.")
    action_description: str = Field(
        default="", description="When someone requested something of the principal: the task, as an imperative phrase. Else empty."
    )
    action_owner: str = Field(default="", description="Who owes the action; the principal's name when it is theirs.")
    action_due_iso: str = Field(
        default="", description="Deadline as ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM) when one was stated, else empty."
    )
    action_due_text: str = Field(default="", description="Deadline exactly as spoken, e.g. 'tomorrow 8PM'. Else empty.")
    needs_human: bool = Field(
        default=False, description="True when a human must see this before or instead of the delegate answering."
    )
    reason: str = Field(default="", description="One short line explaining the choice, for the audit trail.")

    @property
    def speaks(self) -> bool:
        """True when this turn produces speech."""
        return self.turn_type in _SPEAKING_TYPES and bool(self.reply.strip())

    @property
    def has_action(self) -> bool:
        """True when the turn created an action item."""
        return bool(self.action_description.strip())


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT: str = """\
You are DELEG8, an AI meeting delegate attending this call on behalf of \
{principal}. You are speaking live; everything in `reply` is converted straight \
to speech and played into the meeting.

# Who you are
- You ARE {principal}'s delegate, not {principal}. If anyone asks who or what \
  you are, whether you are a real person, or whether you are an AI, say so \
  plainly and warmly: you are {principal}'s AI delegate, sitting in for them.
- For everything else you speak in the first person on {principal}'s behalf: \
  "I applied to X", "I'm studying Y". You represent them, so talk like them - \
  not "Duraikumaran applied to X".
- Never claim to be human. Never pretend {principal} is present.

# How you talk
- One or two sentences. This is speech, not prose. No lists, no markdown, no \
  headings, no emoji, no stage directions.
- Warm, direct, professional. A capable colleague, not a chatbot.
- Talk like a real person on a call: use contractions ("I'm", "I'll", "don't", \
  "that's"), not their spelled-out forms.
- Look at CONVERSATION SO FAR before you write your opener. If you have used a \
  phrase already in this meeting ("Got it", "I don't have that on hand", any \
  fixed template), use a different one this time. Saying the same line twice in \
  one meeting is the single biggest tell that a script is running, not a person.
- A brief natural aside is fine where it genuinely fits ("Hmm, good question", \
  "Oh, sure") - use it occasionally, not on every line, and never for a simple \
  greeting or acknowledgement that needs no thought at all.
- Never open with "As an AI" or "I'm sorry, but". Just answer.
- Do not repeat the question back before answering.

# What you may say
You have two kinds of turn, and the rule differs:

1. CONVERSATIONAL (turn_type: social) - greetings, "can you hear me", "what \
   can you help with", who you are, thanks, acknowledgements. Answer naturally \
   from this prompt. Set states_personal_fact = false. No quotes needed.

2. FACTUAL (turn_type: factual) - ANY claim about {principal}: their studies, \
   grades, employer, skills, projects, applications, availability, history. \
   These may come ONLY from the DOSSIER or DOCUMENTS below. Set \
   states_personal_fact = true and copy the exact sentence(s) or bullet(s) you \
   used into supporting_quotes, character for character. If the sources do not \
   contain the answer, do NOT guess - use turn_type: unknown instead.

3. GENERAL KNOWLEDGE (turn_type: social) - questions that are not about \
   {principal} at all: a definition, a public fact, a unit conversion, how some \
   technology works. Answer briefly from your own knowledge, the way a capable \
   colleague would. Set states_personal_fact = false. Say so if you are unsure. \
   Do not refuse these: refusing to say what a REST API is makes you look \
   broken, not careful.

4. MEETING RECAP (turn_type: social) - "what have we talked about", "recap this \
   call", "what are the action items", "did we agree on anything", "catch me up". \
   Answer from the MEETING SUMMARY SO FAR and CONVERSATION SO FAR sections below - \
   that is what was actually said in this meeting, not a claim about {principal} \
   that needs a dossier citation. Set states_personal_fact = false. If either \
   section below has content, use it; never claim you have no summary available \
   when one was supplied.

5. LEAVE REQUEST (turn_type: leave) - someone explicitly asks you to leave the \
   meeting, hang up, disconnect, or end the call for them. A live call is hard to \
   undo once you leave, so never announce that you are leaving on the first ask: \
   say you understood and ask once, plainly, whether they are sure - for example \
   "Are you sure you'd like me to leave the call now?" Set states_personal_fact = \
   false. A human process outside of you handles the actual confirmation and exit.

# Whose life is the question about?
People slip between "you" and "I" when talking to a delegate, especially in \
interviews. Read every question as being about {principal} unless the speaker \
is plainly asking about themselves or about someone else in the room. "What \
company did I apply to?" asked of you means {principal}'s applications.

# When you do not know (turn_type: unknown)
Say so honestly and promise a follow-up - vary the wording each time rather \
than reusing one sentence all meeting, for example: "I don't have that on \
hand, I'll check with {principal} and get back to you.", "That one I'm not \
sure of, let me confirm with {principal}.", "Good question - I don't know off \
the top of my head, I'll find out." Set states_personal_fact = false. This is \
a good outcome, not a failure. Never invent a number, date, employer, or grade.

# Restricted subjects (turn_type: deflect)
These subjects are off limits to you. If one comes up - in the question OR in \
what you were about to say - decline warmly, in your own words each time, say \
you are flagging it to {principal}, and set needs_human = true.
{red_rules}

These subjects you may DISCUSS but may never COMMIT on (no promises, no dates, \
no agreement). Give context if you have it, then say {principal} will confirm, \
and set needs_human = true.
{yellow_rules}

# Requests and action items (turn_type: commitment)
When someone asks {principal} to do something, or asks you to pass something \
on, or states a deadline for them:
- Acknowledge it out loud and confirm you will pass it on. Be specific about \
  what you understood, and vary your opener each time instead of reaching for \
  the same one - "Got it", "Understood", "Noted", "Will do", "Absolutely", \
  "Sure thing" all work: "Understood - I'll have {principal} call you tomorrow \
  at 8pm on WhatsApp." / "Noted, I'll make sure that reaches {principal} today."
- Fill action_description (imperative phrase), action_owner, and the deadline \
  in BOTH action_due_iso (ISO-8601) and action_due_text (as spoken).
- A request spread over several utterances counts: use the conversation so far \
  to assemble it, and if a later utterance only adds a time, restate the whole \
  action with that time.

# When to stay quiet (turn_type: silent)
- {principal} is speaking for themselves.
- Someone else in the room is clearly answering.
- The utterance is addressed to another named person.
- It is filler with no request in it and no one is waiting on you.
Otherwise, respond. Silence is the exception, not the default - a delegate that \
sits mute through a meeting is worse than useless.

# How many people are on this call
{meeting_mode_guidance}

# Time
The current meeting time is {now}. Resolve "tomorrow", "next Tuesday" and \
similar against it when filling action_due_iso.
"""

_USER_TEMPLATE: str = """\
# DOSSIER ({principal})
{dossier}

# DOCUMENTS RETRIEVED FOR THIS QUESTION
{documents}

# MEETING SUMMARY SO FAR (decisions, action items, open questions)
{meeting_summary}

# CONVERSATION SO FAR
{history}

# THE UTTERANCE YOU ARE RESPONDING TO
{speaker}: {utterance}

Decide the turn type and write the reply."""

_NO_DOSSIER = "(No dossier has been compiled. You know nothing about the principal beyond the documents below.)"
_NO_DOCUMENTS = "(No documents were retrieved for this utterance.)"
_NO_HISTORY = "(This is the first thing said.)"
_NO_SUMMARY = "(Nothing notable has been recorded yet.)"
_NO_RULES = "- (none configured)"


def _meeting_mode_guidance(participant_count: int | None, principal: str) -> str:
    """How eagerly the delegate should volunteer answers, based on headcount.

    A one-on-one call has no one else for the other person to be talking to, so
    the current default-to-respond behaviour is correct there. Once a third
    person joins, that same default makes the delegate answer questions meant
    for someone else in the room; ``participant_count`` (read live from the
    Meet UI when available) is what tells the two situations apart. ``None``
    means the headcount could not be read, so this falls back to the one-on-one
    text - the behaviour this product already had before headcount tracking
    existed, rather than guessing.
    """
    if participant_count is not None and participant_count >= 3:
        return (
            f"There are {participant_count} people on this call right now (you included), so you "
            "are one voice among several, not the room's default respondent.\n"
            f"- FIRST check: does the question name you (\"DELEG8\") or {principal} directly? If so, "
            "say SOMETHING - never turn_type: silent - even when the same sentence ALSO asks what "
            "\"everyone\"/\"the group\"/\"you all\" think. Being named is what makes it your turn. If "
            f"you do not actually know {principal}'s personal view on it, say so honestly (turn_type: "
            "unknown) rather than going quiet - a named question left completely unanswered looks broken.\n"
            "- Otherwise, only speak if nobody else has answered and you hold a confident, dossier- "
            "or document-grounded answer that only you would have, or the question is about you "
            "specifically.\n"
            "- A question thrown open to the whole group with no one named should be left for a "
            "human, UNLESS the first check above already said to answer."
        )
    return (
        "This is effectively a one-on-one call: it is just you and the other person on the line, "
        "with no one else for them to be talking to. Treat nearly everything they say as directed "
        "at you and answer readily rather than waiting to be singled out by name."
    )


def _render_rules(rules: list[PolicyRule]) -> str:
    if not rules:
        return _NO_RULES
    return "\n".join(f"- {r.description} (triggers include: {', '.join(r.triggers[:6])})" for r in rules)


@dataclass(frozen=True)
class PlannerInput:
    """Everything one planning call needs."""

    context: MeetingContext
    profile: UserProfile
    dossier: str
    documents: str
    history: list[tuple[str, str]]
    """Recent ``(speaker, text)`` pairs, oldest first."""
    meeting_summary: str = ""
    """Structured recap material (topic, decisions, action items, open questions)
    covering the *whole* meeting, not just the rolling history window - what makes
    "can you recap what we've discussed" answerable even late in a long meeting."""

    def render_history(self, limit: int = 10) -> str:
        """The last ``limit`` turns as a transcript block."""
        recent = self.history[-limit:]
        if not recent:
            return _NO_HISTORY
        return "\n".join(f"{speaker}: {text}" for speaker, text in recent)


class ResponsePlanner:
    """Turns a meeting utterance into a :class:`PlannedReply` with one model call."""

    def __init__(self, llm: LlmClient) -> None:
        """Bind to a configured language model. Without one there is no planner."""
        self._llm = llm

    def describe(self) -> str:
        """Status line."""
        return f"planner: {self._llm.describe()}"

    def build_system_prompt(self, profile: UserProfile, *, now: str, participant_count: int | None = None) -> str:
        """The system prompt for ``profile``, including its restricted subjects."""
        red = [r for r in profile.policy if r.tier is PolicyTier.RED]
        yellow = [r for r in profile.policy if r.tier is PolicyTier.YELLOW]
        return _SYSTEM_PROMPT.format(
            principal=profile.display_name,
            red_rules=_render_rules(red),
            yellow_rules=_render_rules(yellow),
            now=now,
            meeting_mode_guidance=_meeting_mode_guidance(participant_count, profile.display_name),
        )

    async def plan(self, request: PlannerInput) -> PlannedReply:
        """Ask the model what to say.

        Raises:
            LlmError: on any failure; the caller decides how to degrade.
        """
        profile, context = request.profile, request.context
        system = self.build_system_prompt(
            profile,
            now=context.timestamp.strftime("%A %d %B %Y, %H:%M UTC"),
            participant_count=context.participant_count,
        )
        user = _USER_TEMPLATE.format(
            principal=profile.display_name,
            dossier=request.dossier.strip() or _NO_DOSSIER,
            documents=request.documents.strip() or _NO_DOCUMENTS,
            meeting_summary=request.meeting_summary.strip() or _NO_SUMMARY,
            history=request.render_history(),
            speaker=context.speaker or "Someone",
            utterance=context.transcript,
        )
        reply = await self._llm.generate(
            system=system, user=user, schema=PlannedReply, role=ModelRole.FAST, max_output_tokens=1536,
        )
        logger.info(
            "planned %s (confidence %.2f, personal_fact=%s, action=%s): %r",
            reply.turn_type.value, reply.confidence, reply.states_personal_fact,
            bool(reply.action_description), reply.reply[:80],
        )
        return reply


# --------------------------------------------------------------------------- #
# Output guard
# --------------------------------------------------------------------------- #

_SPEECH_ARTEFACTS = re.compile(r"^\s*(?:as an ai\b[^.!?]*[.!?]\s*|i'?m sorry,?\s+but\s+)", re.IGNORECASE)
_MARKDOWN_NOISE = re.compile(r"[*_`#]+")


class GuardVerdict(str, Enum):
    """What the guard did to a planned reply."""

    PASSED = "passed"
    """Spoken as planned."""
    REWRITTEN = "rewritten"
    """A claim did not verify; replaced with an honest deferral."""
    BLOCKED = "blocked"
    """A restricted subject appeared in the output; escalated instead."""


@dataclass(frozen=True)
class GuardedReply:
    """A planned reply after the guard has run."""

    reply: PlannedReply
    verdict: GuardVerdict
    detail: str
    spoken_text: str | None
    """What will actually be said, or ``None`` when nothing will be."""


def clean_for_speech(text: str) -> str:
    """Strip markdown, hedging preambles and stray whitespace from a spoken line."""
    cleaned = _MARKDOWN_NOISE.sub("", text).strip()
    cleaned = _SPEECH_ARTEFACTS.sub("", cleaned).strip()
    cleaned = " ".join(cleaned.split())
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


class OutputGuard:
    """Last check before the delegate speaks.

    Two independent jobs, both on the *output* rather than the question:

    * **Grounding.** When the planner says the reply asserts a personal fact,
      every supporting quote must appear verbatim in the sources that were
      supplied. Models are good at staying grounded and bad at being reliably
      grounded; this makes the difference programmatic rather than a matter of
      how well the prompt happened to land.
    * **Restriction.** A RED topic can surface in an answer even when the
      question looked harmless - ask about a job application and the dossier
      may volunteer salary. Input-side checks miss that entirely.
    """

    def __init__(self, deferral: str | None = None) -> None:
        """``deferral`` overrides the sentence spoken when a claim fails to verify."""
        self._deferral = deferral

    def deferral_for(self, profile: UserProfile) -> str:
        """The honest fallback sentence for this principal."""
        if self._deferral:
            return self._deferral
        return (
            f"I don't want to give you the wrong detail on that one - "
            f"let me check with {profile.display_name} and come straight back to you."
        )

    def check(self, reply: PlannedReply, *, profile: UserProfile, sources: str) -> GuardedReply:
        """Apply both checks and return what may actually be said."""
        if not reply.speaks:
            return GuardedReply(reply=reply, verdict=GuardVerdict.PASSED, detail="nothing to say", spoken_text=None)

        spoken = clean_for_speech(reply.reply)
        if not spoken:
            return GuardedReply(reply=reply, verdict=GuardVerdict.PASSED, detail="reply was empty after cleaning", spoken_text=None)

        # -- restriction: does the OUTPUT touch a RED subject? --------------
        breached = self._red_breach(spoken, profile)
        if breached is not None and reply.turn_type is not TurnType.DEFLECT:
            logger.warning("guard blocked a reply touching RED topic %r", breached.topic)
            return GuardedReply(
                reply=reply,
                verdict=GuardVerdict.BLOCKED,
                detail=f"output touched RED topic '{breached.topic}'",
                spoken_text=None,
            )

        # -- grounding: do the quotes verify? ------------------------------
        if reply.states_personal_fact:
            if not reply.supporting_quotes:
                logger.warning("guard rewrote a factual reply with no supporting quotes")
                return GuardedReply(
                    reply=reply,
                    verdict=GuardVerdict.REWRITTEN,
                    detail="factual claim with no supporting quote",
                    spoken_text=self.deferral_for(profile),
                )
            if not verify_citations(reply.supporting_quotes, sources):
                logger.warning("guard rewrote a factual reply whose quotes did not verify: %r", reply.supporting_quotes[:2])
                return GuardedReply(
                    reply=reply,
                    verdict=GuardVerdict.REWRITTEN,
                    detail="supporting quotes did not appear verbatim in the dossier or documents",
                    spoken_text=self.deferral_for(profile),
                )

        return GuardedReply(reply=reply, verdict=GuardVerdict.PASSED, detail="grounded and permitted", spoken_text=spoken)

    @staticmethod
    def _red_breach(spoken: str, profile: UserProfile) -> PolicyRule | None:
        """The RED rule whose trigger appears in the spoken text, if any."""
        lowered = f" {' '.join(spoken.lower().split())} "
        for rule in profile.policy:
            if rule.tier is not PolicyTier.RED:
                continue
            for trigger in rule.triggers:
                if f" {trigger} " in lowered or lowered.startswith(f" {trigger}") or lowered.endswith(f"{trigger} "):
                    return rule
        return None


def build_planner(llm: LlmClient | None) -> ResponsePlanner | None:
    """A planner when a model is configured, else ``None``."""
    if llm is None:
        logger.warning("no language model configured; the delegate cannot plan replies")
        return None
    return ResponsePlanner(llm)


__all__ = [
    "GuardVerdict",
    "GuardedReply",
    "LlmError",
    "OutputGuard",
    "PlannedReply",
    "PlannerInput",
    "ResponsePlanner",
    "TurnType",
    "build_planner",
    "clean_for_speech",
]
