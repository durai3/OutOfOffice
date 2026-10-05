"""
Policy Router Brain - the decision state machine.

Every transcript snippet walks the stages below from top to bottom. Each
stage either falls through to the next or exits to one of three terminal
states. The walk is deterministic and every transition is recorded in the
returned :class:`PolicyDecision.trace`.

    RECEIVED
      |-- transcript is empty ---------------------------------> SILENT
      v
    FLOOR_CHECK
      |-- the principal is the speaker ------------------------> SILENT
      |-- someone has already started responding --------------> SILENT
      v
    ADDRESS_CHECK
      |-- utterance is addressed to someone else --------------> SILENT
      v
    TOPIC_CLASSIFICATION            (all matching rules collected)
      |-- no topic + social small talk ------------------------> SILENT
      |-- no topic + a request + documents answer it ----------> AUTO_REPLY (documented_facts)
      |-- no topic + directly addressed + a request -----------> ESCALATE
      |-- no topic -------------------------------------------> SILENT
      v
    POLICY_LOOKUP                   (most restrictive tier wins)
      |-- RED tier (even if merely mentioned) -----------------> ESCALATE
      |-- not phrased as a question or request ----------------> SILENT
      |-- YELLOW tier ----------------------------------------> ESCALATE
      v  (GREEN tier)
    KNOWLEDGE_LOOKUP                (profile facts, then the document index)
      |-- grounded answer found -------------------------------> AUTO_REPLY
      `-- UNKNOWN_CONTEXT ------------------------------------> ESCALATE

Design rules baked into the ordering:

* Safety beats helpfulness: when topics overlap, the most restrictive tier
  governs; when a GREEN topic has no grounded answer we escalate rather
  than improvise.
* RED topics escalate as soon as they are raised, whether or not anyone
  asked a question, because the human must know the subject came up.
* Silence is the default for anything that is not clearly a request aimed
  at the principal.
* Knowledge is pluggable (:class:`KnowledgeSource`). Sprint 4 adds the
  document index (RAG); the engine only ever speaks text a source grounded,
  and records the evidence in :attr:`PolicyDecision.grounding`.
* The document path for uncovered questions is opt-in per principal: the
  profile must carry a GREEN rule with topic ``documented_facts``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass
from enum import Enum

from src.brain.intent import RequestIntentClassifier
from src.brain.knowledge import (
    DOCUMENTED_FACTS_TOPIC,
    GroundedAnswer,
    KnowledgeSource,
    ProfileKnowledge,
)
from src.brain.models import (
    DecisionState,
    Grounding,
    MeetingContext,
    PolicyDecision,
    PolicyRule,
    PolicyTier,
    TraceStep,
    UserProfile,
)
from src.brain.profile_store import ProfileStore
from src.brain.text import matched_phrases, normalise

logger = logging.getLogger("deleg8.brain")


class EngineStage(str, Enum):
    """Non-terminal stages of the state machine, in evaluation order."""

    RECEIVED = "RECEIVED"
    FLOOR_CHECK = "FLOOR_CHECK"
    ADDRESS_CHECK = "ADDRESS_CHECK"
    TOPIC_CLASSIFICATION = "TOPIC_CLASSIFICATION"
    POLICY_LOOKUP = "POLICY_LOOKUP"
    KNOWLEDGE_LOOKUP = "KNOWLEDGE_LOOKUP"


# --------------------------------------------------------------------------- #
# Lexicons. Deliberately small and explicit: the router is rule-based so that
# every decision can be explained by pointing at a phrase in this file.
# --------------------------------------------------------------------------- #

_REQUEST_MARKERS: tuple[str, ...] = (
    "can you",
    "could you",
    "can we",
    "could we",
    "would you",
    "will you",
    "are you able",
    "please",
    "we need",
    "i need",
    "tell us",
    "tell me",
    "give us",
    "give me",
    "walk us through",
    "walk me through",
    "any update",
    "let us know",
    "we'd like",
    "we would like",
    "what's",
    "what is",
    "what are",
    "which",
    "who is",
    "who's",
    "where is",
    "where's",
    "when is",
    "when's",
    "when does",
    "when will",
    "when can",
    "how soon",
    "how many",
    "how much",
    "how does",
    "how do",
    "how did",
    "how can",
    "how should",
    "how long",
    "is it possible",
    "do you",
    "does it",
    "have you",
    "why did",
    "why does",
    "why is",
    "why are",
    "why would",
    "why can't",
    "why won't",
    "why not",
    "what happened",
    "what caused",
    "what led to",
    "what changed",
    "who did",
    "who was",
    "who owns",
    "who's handling",
    "when did",
    "when was",
    "where did",
    "where was",
    "where are we",
    "where do we stand",
    "any word on",
    "any news on",
    "status on",
    "catch us up",
    "catch me up",
    "bring us up to speed",
    "update us on",
    "break down",
    "run me through",
    "explain",
    "clarify",
    "elaborate on",
)

_SMALL_TALK_MARKERS: tuple[str, ...] = (
    "weekend",
    "how are you",
    "how's it going",
    "how is it going",
    "how was your",
    "how's your",
    "vacation",
    "holiday",
    "weather",
    "lunch",
    "coffee",
    "birthday",
    "congrats",
    "congratulations",
    "traffic",
    "flight",
)

_INTERROGATIVE_WORDS: tuple[str, ...] = ("what", "why", "who", "when", "where", "which", "how")
"""Cheap pre-filter for :meth:`PolicyEngine._worth_checking`: bounds how often the
optional LLM intent classifier is consulted on utterances with no lexical topic."""

_GREETING_PATTERN = r"hey|hi|hello|yo|good\s+(?:morning|afternoon|evening)"

# Words that can start a sentence with a capital letter and a comma but are
# never a person's name. Guards the leading-vocative heuristic.
_NON_NAME_WORDS: frozenset[str] = frozenset(
    {
        "hey", "hi", "hello", "yo", "good", "ok", "okay", "so", "well", "yes", "no",
        "sure", "thanks", "thank", "right", "great", "actually", "also", "and", "but",
        "what", "when", "where", "who", "why", "how", "can", "could", "would", "will",
        "should", "is", "are", "do", "does", "did", "the", "a", "an", "i", "we", "you",
        "they", "it", "this", "that", "there", "first", "next", "now", "look", "listen",
        "honestly", "unfortunately", "however", "anyway", "alright", "again",
    }
)

# Group addresses are not "someone else", so they never silence the delegate.
_GROUP_ADDRESS_WORDS: frozenset[str] = frozenset(
    {"everyone", "everybody", "all", "team", "folks", "guys", "y'all", "people"}
)

_DELEGATE_NAMES: frozenset[str] = frozenset({"deleg8", "delegate", "the delegate"})
"""Ways someone might address the delegate itself by name. Without this, "Hey DELEG8,
what's your name?" is detected as an addressee (the leading-vocative heuristic below
does not require a known name, only a greeting plus a capitalised word) and then
silenced by ADDRESS_CHECK because "deleg8" is not in ``profile.names`` - the delegate
being asked its own name was the one case that heuristic could never let through."""

_LEADING_VOCATIVE_RE = re.compile(
    rf"^\W*(?:(?P<greeting>{_GREETING_PATTERN})[\s,]+)?"
    rf"(?P<name>[A-Za-z][A-Za-z0-9'\-]*)(?P<punct>\s*[,:;!\-–—])?",
    re.IGNORECASE,
)
"""Name characters include digits so "DELEG8" is captured whole rather than
truncated to "DELEG" before it is compared against :data:`_DELEGATE_NAMES`."""

_TRAILING_VOCATIVE_RE = re.compile(
    r",\s*(?P<name>[A-Za-z][A-Za-z0-9'\-]*)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


_FILLER_UTTERANCES: frozenset[str] = frozenset(
    {
        "ok", "okay", "mm", "mhm", "mm hm", "uh huh", "uh-huh", "yeah", "yep", "yes", "no", "nope",
        "right", "sure", "cool", "nice", "great", "thanks", "thank you", "got it", "i see", "sorry",
        "hmm", "ah", "oh", "wow", "exactly", "true", "alright", "perfect", "fine", "indeed",
    }
)
"""Acknowledgement noise. Answering these makes the delegate feel like it is
talking over people, and every one of them would otherwise cost a model call -
which matters on a rate-limited key."""


@dataclass(frozen=True)
class TopicMatch:
    """A policy rule that fired, together with the trigger phrases that fired it."""

    rule: PolicyRule
    triggers: tuple[str, ...]


class FloorVerdict(str, Enum):
    """Whether the delegate should take this turn."""

    RESPOND = "RESPOND"
    """The floor is open and the delegate should plan a reply."""
    LISTEN = "LISTEN"
    """Someone else owns this turn; say nothing and keep listening."""


@dataclass(frozen=True)
class FloorCheck:
    """Result of :meth:`PolicyEngine.check_floor`."""

    verdict: FloorVerdict
    reason: str
    addressee: str | None = None

    @property
    def should_respond(self) -> bool:
        """True when the delegate should plan a reply."""
        return self.verdict is FloorVerdict.RESPOND


@dataclass(frozen=True)
class SensitivityCheck:
    """Result of :meth:`PolicyEngine.check_sensitivity`."""

    tier: PolicyTier | None
    rule: PolicyRule | None
    triggers: tuple[str, ...]

    @property
    def restricted(self) -> bool:
        """True when a RED or YELLOW topic was matched."""
        return self.tier in {PolicyTier.RED, PolicyTier.YELLOW}

    @property
    def forbidden(self) -> bool:
        """True when a RED topic was matched."""
        return self.tier is PolicyTier.RED

    def describe(self) -> str:
        """Short label for traces."""
        if self.rule is None:
            return "no policy topic matched"
        return f"{self.rule.topic}({self.tier.value if self.tier else '-'})"


@dataclass(frozen=True)
class Signals:
    """Situational features extracted once and consumed by every stage."""

    text: str
    """Normalised transcript."""
    addressee: str | None
    """Name the utterance is addressed to, if one could be determined."""
    directly_addressed: bool
    """True when the addressee is the principal."""
    is_request: bool
    """True when the utterance asks for something (question or request phrasing)."""
    is_small_talk: bool
    """True when social-chatter markers are present."""


@dataclass(frozen=True)
class _KnowledgeQuery:
    """The walk paused at KNOWLEDGE_LOOKUP; the knowledge source must be consulted."""

    query: str
    """Normalised utterance to ground."""
    topic: str
    """Policy topic the answer must be grounded for."""
    signals: Signals
    match: TopicMatch | None
    """The governing GREEN rule, or ``None`` on the semantic (no lexical topic) path."""
    facts_rule: PolicyRule | None
    """The profile's ``documented_facts`` rule when on the semantic path."""


class _Trace:
    """Append-only record of state-machine transitions."""

    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    def record(self, stage: EngineStage, outcome: str) -> None:
        """Append one transition."""
        self.steps.append(TraceStep(stage=stage.value, outcome=outcome))


class PolicyEngine:
    """Routes transcript snippets to AUTO_REPLY, SILENT, or ESCALATE.

    The engine is stateless between calls; all per-evaluation state lives in
    the :class:`Signals` and :class:`_Trace` objects created inside
    :meth:`evaluate`, so a single instance is safe to share across requests.

    Use :meth:`evaluate_async` from the event loop: the document knowledge
    source embeds the query on the CPU, and the async path keeps that work
    off the loop. :meth:`evaluate` is the synchronous equivalent for scripts
    and worker threads.
    """

    def __init__(
        self,
        store: ProfileStore,
        knowledge: KnowledgeSource | None = None,
        intent_classifier: RequestIntentClassifier | None = None,
    ) -> None:
        """Bind the engine to the policy store, the knowledge source, and the optional intent classifier.

        ``intent_classifier`` (Sprint 7), when given, is consulted by
        :meth:`evaluate_async` alone, and only for utterances the deterministic
        request lexicon did not already recognise. See :meth:`_maybe_classify_intent`.
        """
        self._store = store
        self._knowledge: KnowledgeSource = knowledge if knowledge is not None else ProfileKnowledge()
        self._intent_classifier = intent_classifier

    @property
    def knowledge(self) -> KnowledgeSource:
        """The knowledge source consulted for AUTO_REPLY candidates."""
        return self._knowledge

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def evaluate(self, context: MeetingContext) -> PolicyDecision:
        """Run ``context`` through the state machine and return the decision.

        Synchronous: never consults the optional LLM intent classifier (there is
        no sync Anthropic client), so ``is_request`` is decided by the lexicon
        alone. Scripts and worker threads get this deterministic behaviour.

        Raises:
            ProfileNotFoundError: if ``context.user_id`` has no policy.
        """
        profile = self._store.get_profile(context.user_id)
        trace = _Trace()
        signals = self._extract_signals(context, profile)
        routed = self._route(context, profile, trace, signals)
        if isinstance(routed, PolicyDecision):
            return self._log(context, routed)
        answer = self._knowledge.lookup(routed.query, routed.topic, profile)
        return self._log(context, self._resolve_knowledge(routed, answer, profile, trace))

    async def evaluate_async(self, context: MeetingContext) -> PolicyDecision:
        """Like :meth:`evaluate`, but awaits the knowledge source off the event loop.

        Also the only path that consults the optional LLM intent classifier: when
        the lexicon alone did not recognise a request, and flipping ``is_request``
        could actually change the outcome, one extra check is awaited directly on
        the event loop before routing.

        Raises:
            ProfileNotFoundError: if ``context.user_id`` has no policy.
        """
        profile = self._store.get_profile(context.user_id)
        trace = _Trace()
        signals = self._extract_signals(context, profile)
        signals = await self._maybe_classify_intent(context, profile, signals, trace)
        routed = self._route(context, profile, trace, signals)
        if isinstance(routed, PolicyDecision):
            return self._log(context, routed)
        answer = await self._knowledge.lookup_async(routed.query, routed.topic, profile)
        return self._log(context, self._resolve_knowledge(routed, answer, profile, trace))

    async def _maybe_classify_intent(
        self, context: MeetingContext, profile: UserProfile, signals: Signals, trace: _Trace
    ) -> Signals:
        """Ask the optional LLM classifier whether an unmarked utterance is a request.

        Only consulted when the deterministic lexicon said ``False`` and
        :meth:`_worth_checking` says a ``True`` verdict could actually change
        the routing outcome, which bounds how much of a normal meeting's
        non-question chatter triggers a network call.
        """
        if (
            not context.transcript
            or signals.is_request
            or self._intent_classifier is None
            or not self._worth_checking(signals, profile)
        ):
            return signals
        if await self._intent_classifier.looks_like_request(context.transcript):
            trace.record(EngineStage.TOPIC_CLASSIFICATION, "LLM intent classifier: recognised as a request")
            return dataclasses.replace(signals, is_request=True)
        return signals

    def _worth_checking(self, signals: Signals, profile: UserProfile) -> bool:
        """True when a ``True`` verdict from the intent classifier could change the outcome.

        Mirrors the checks :meth:`_route` will make: a lexical topic matched
        (YELLOW would otherwise go SILENT instead of ESCALATE; GREEN would go
        SILENT instead of a knowledge lookup) - volume here is naturally bounded
        by how often the profile's specific topics come up. When no topic
        matched, the semantic ``documented_facts`` path is only worth checking
        if the utterance also contains an interrogative word: almost every
        ordinary meeting sentence is not small talk either, so without this an
        LLM call would fire on nearly all of it.
        """
        if signals.is_small_talk:
            return False
        matches = self._classify_topics(signals.text, profile)
        if matches:
            return self._most_restrictive(matches).rule.tier is not PolicyTier.RED
        facts_rule = profile.rule_for(DOCUMENTED_FACTS_TOPIC)
        if facts_rule is None or facts_rule.tier is not PolicyTier.GREEN:
            return False
        return bool(matched_phrases(signals.text, _INTERROGATIVE_WORDS))

    @staticmethod
    def _log(context: MeetingContext, decision: PolicyDecision) -> PolicyDecision:
        logger.info(
            "decision=%s topic=%s tier=%s user=%s meeting=%s grounding=%s reason=%s",
            decision.state.value,
            decision.topic,
            decision.tier.value if decision.tier else None,
            context.user_id,
            context.meeting_id,
            decision.grounding.describe() if decision.grounding else None,
            decision.reason,
        )
        return decision

    # ------------------------------------------------------------------ #
    # State machine
    # ------------------------------------------------------------------ #

    def _route(
        self, context: MeetingContext, profile: UserProfile, trace: _Trace, signals: Signals
    ) -> PolicyDecision | _KnowledgeQuery:
        """Walk the stages in order until a terminal state or a knowledge lookup is reached.

        ``signals`` is computed by the caller (and, on the async path, possibly
        refined by the optional LLM intent classifier) before routing begins.
        """
        # RECEIVED -------------------------------------------------------
        if not context.transcript:
            trace.record(EngineStage.RECEIVED, "empty transcript")
            return self._decide(
                DecisionState.SILENT, "Nothing was said; there is nothing to evaluate.", trace
            )
        trace.record(EngineStage.RECEIVED, f"transcript of {len(context.transcript)} chars")

        # FLOOR_CHECK ----------------------------------------------------
        if context.speaker and normalise(context.speaker) in profile.names:
            trace.record(EngineStage.FLOOR_CHECK, "principal is speaking")
            return self._decide(
                DecisionState.SILENT,
                f"{profile.display_name} is speaking for themselves; the delegate stays quiet.",
                trace,
            )
        if context.responder:
            trace.record(EngineStage.FLOOR_CHECK, f"{context.responder} is already responding")
            return self._decide(
                DecisionState.SILENT,
                f"{context.responder} is already responding; the delegate does not interrupt.",
                trace,
            )
        trace.record(EngineStage.FLOOR_CHECK, "floor is open")

        # ADDRESS_CHECK --------------------------------------------------
        if signals.addressee and not signals.directly_addressed:
            trace.record(EngineStage.ADDRESS_CHECK, f"addressed to {signals.addressee}")
            return self._decide(
                DecisionState.SILENT,
                f"The utterance is addressed to {signals.addressee}, not to {profile.display_name}.",
                trace,
            )
        trace.record(
            EngineStage.ADDRESS_CHECK,
            "addressed to principal" if signals.directly_addressed else "no specific addressee",
        )

        # TOPIC_CLASSIFICATION ------------------------------------------
        matches = self._classify_topics(signals.text, profile)
        if not matches:
            trace.record(EngineStage.TOPIC_CLASSIFICATION, "no policy topic matched")
            if signals.is_small_talk:
                return self._decide(
                    DecisionState.SILENT, "Social conversation; not a business request.", trace
                )
            facts_rule = profile.rule_for(DOCUMENTED_FACTS_TOPIC)
            if signals.is_request and facts_rule is not None and facts_rule.tier is PolicyTier.GREEN:
                trace.record(
                    EngineStage.TOPIC_CLASSIFICATION,
                    f"request with no lexical topic; consulting documents under '{DOCUMENTED_FACTS_TOPIC}'",
                )
                return _KnowledgeQuery(
                    query=signals.text,
                    topic=DOCUMENTED_FACTS_TOPIC,
                    signals=signals,
                    match=None,
                    facts_rule=facts_rule,
                )
            return self._decide_uncovered(signals, profile, trace)
        trace.record(
            EngineStage.TOPIC_CLASSIFICATION,
            "matched " + ", ".join(f"{m.rule.topic}({m.rule.tier.value})" for m in matches),
        )

        # POLICY_LOOKUP --------------------------------------------------
        governing = self._most_restrictive(matches)
        rule = governing.rule
        trace.record(EngineStage.POLICY_LOOKUP, f"governing topic {rule.topic} tier {rule.tier.value}")

        if rule.tier is PolicyTier.RED:
            return self._decide(
                DecisionState.ESCALATE,
                f"'{rule.description}' is RED: the delegate never handles this. "
                "Escalating to a human.",
                trace,
                match=governing,
            )
        if not signals.is_request:
            trace.record(EngineStage.POLICY_LOOKUP, "topic mentioned but nothing was asked")
            return self._decide(
                DecisionState.SILENT,
                f"'{rule.topic}' was mentioned but no question or request was made.",
                trace,
                match=governing,
            )
        if rule.tier is PolicyTier.YELLOW:
            return self._decide(
                DecisionState.ESCALATE,
                f"'{rule.description}' is YELLOW: human approval is required before "
                "the delegate commits.",
                trace,
                match=governing,
            )

        # KNOWLEDGE_LOOKUP (GREEN) --------------------------------------
        return _KnowledgeQuery(
            query=signals.text, topic=rule.topic, signals=signals, match=governing, facts_rule=None
        )

    def _decide_uncovered(self, signals: Signals, profile: UserProfile, trace: _Trace) -> PolicyDecision:
        """Terminal states for an utterance no policy topic (lexical or semantic) covers."""
        if signals.directly_addressed and signals.is_request:
            return self._decide(
                DecisionState.ESCALATE,
                f"A request was made directly to {profile.display_name} on a topic the "
                "Representation Policy does not cover; a human must respond.",
                trace,
            )
        return self._decide(
            DecisionState.SILENT,
            "No Representation Policy topic applies to this utterance.",
            trace,
        )

    def _resolve_knowledge(
        self,
        pending: _KnowledgeQuery,
        answer: GroundedAnswer | None,
        profile: UserProfile,
        trace: _Trace,
    ) -> PolicyDecision:
        """Finish the walk once the knowledge source has answered (or not)."""
        if pending.match is not None:
            rule = pending.match.rule
            if answer is None:
                trace.record(EngineStage.KNOWLEDGE_LOOKUP, "UNKNOWN_CONTEXT: no grounded answer for this subject")
                return self._decide(
                    DecisionState.ESCALATE,
                    f"'{rule.description}' is GREEN, but no knowledge source holds a grounded "
                    "answer for this subject. Escalating rather than guessing.",
                    trace,
                    match=pending.match,
                )
            trace.record(EngineStage.KNOWLEDGE_LOOKUP, f"grounded answer: {answer.grounding.describe()}")
            return self._decide(
                DecisionState.AUTO_REPLY,
                f"'{rule.description}' is GREEN and a grounded answer exists "
                f"({answer.grounding.source.value}: {answer.grounding.subject}). The delegate may respond.",
                trace,
                match=pending.match,
                suggested_response=answer.answer,
                grounding=answer.grounding,
            )

        # Semantic path: no lexical topic, but the documents were asked.
        facts_rule = pending.facts_rule
        assert facts_rule is not None
        if answer is None:
            trace.record(EngineStage.KNOWLEDGE_LOOKUP, "UNKNOWN_CONTEXT: documents do not answer this")
            return self._decide_uncovered(pending.signals, profile, trace)
        trace.record(
            EngineStage.TOPIC_CLASSIFICATION,
            f"semantic match: {facts_rule.topic}({facts_rule.tier.value}) via {answer.grounding.describe()}",
        )
        trace.record(EngineStage.POLICY_LOOKUP, f"governing topic {facts_rule.topic} tier {facts_rule.tier.value}")
        trace.record(EngineStage.KNOWLEDGE_LOOKUP, f"grounded answer: {answer.grounding.describe()}")
        return self._decide(
            DecisionState.AUTO_REPLY,
            f"'{facts_rule.description}' is GREEN and the documents ground an answer "
            f"({answer.grounding.subject}). The delegate may respond.",
            trace,
            match=TopicMatch(rule=facts_rule, triggers=()),
            suggested_response=answer.answer,
            grounding=answer.grounding,
        )

    # ------------------------------------------------------------------ #
    # Signal extraction
    # ------------------------------------------------------------------ #

    def _extract_signals(self, context: MeetingContext, profile: UserProfile) -> Signals:
        """Compute the situational features every stage relies on."""
        text = normalise(context.transcript)
        known_names = profile.names | {normalise(p) for p in context.participants if p.strip()}

        if context.addressed_to and context.addressed_to.strip():
            addressee: str | None = context.addressed_to.strip()
        else:
            addressee = self._detect_addressee(context.transcript, known_names)

        directly_addressed = addressee is not None and (
            normalise(addressee) in profile.names or normalise(addressee) in _DELEGATE_NAMES
        )
        is_request = text.endswith("?") or bool(matched_phrases(text, _REQUEST_MARKERS))
        is_small_talk = bool(matched_phrases(text, _SMALL_TALK_MARKERS))

        return Signals(
            text=text,
            addressee=addressee,
            directly_addressed=directly_addressed,
            is_request=is_request,
            is_small_talk=is_small_talk,
        )

    @staticmethod
    def _detect_addressee(raw_text: str, known_names: frozenset[str] | set[str]) -> str | None:
        """Heuristically extract who an utterance is aimed at.

        A leading vocative ("Hey Sarah, ...", "Durai: ...") is accepted when the
        name is a known participant, or when it follows a greeting and is
        capitalised. A trailing vocative ("..., Sarah?") is accepted only for
        known participants. Group addresses ("Hey everyone") yield ``None``.
        """
        leading = _LEADING_VOCATIVE_RE.match(raw_text)
        if leading:
            name = leading.group("name")
            key = name.lower()
            greeting = leading.group("greeting") is not None
            punct = leading.group("punct") is not None
            if key in _GROUP_ADDRESS_WORDS:
                return None
            if key not in _NON_NAME_WORDS:
                if key in known_names and (greeting or punct):
                    return name
                if greeting and name[:1].isupper():
                    return name

        trailing = _TRAILING_VOCATIVE_RE.search(raw_text)
        if trailing:
            name = trailing.group("name")
            if name.lower() in known_names:
                return name
        return None

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_topics(text: str, profile: UserProfile) -> list[TopicMatch]:
        """Return every policy rule whose triggers appear in ``text``."""
        matches: list[TopicMatch] = []
        for rule in profile.policy:
            fired = matched_phrases(text, rule.triggers)
            if fired:
                matches.append(TopicMatch(rule=rule, triggers=fired))
        return matches

    @staticmethod
    def _most_restrictive(matches: list[TopicMatch]) -> TopicMatch:
        """Pick the safest governing topic: highest tier, then most trigger hits."""
        return max(matches, key=lambda m: (m.rule.tier.restrictiveness, len(m.triggers)))

    # ------------------------------------------------------------------ #
    # Floor control and sensitivity (the planner-era entry points)
    # ------------------------------------------------------------------ #

    def check_floor(self, context: MeetingContext) -> FloorCheck:
        """Should the delegate take this turn at all? Deterministic, sub-millisecond.

        This is the *only* gate that now precedes the response planner, and it
        answers a much narrower question than the legacy state machine did.
        The old walk asked "may I speak?" and defaulted to no, which is how a
        delegate ended up mute when asked its own name. This asks "does the
        floor belong to someone else?" and defaults to yes.

        Raises:
            ProfileNotFoundError: if ``context.user_id`` has no policy.
        """
        profile = self._store.get_profile(context.user_id)
        text = normalise(context.transcript)

        if not context.transcript.strip():
            return FloorCheck(FloorVerdict.LISTEN, "nothing was said")
        if context.speaker and normalise(context.speaker) in profile.names:
            return FloorCheck(FloorVerdict.LISTEN, f"{profile.display_name} is speaking for themselves")
        if context.responder:
            return FloorCheck(FloorVerdict.LISTEN, f"{context.responder} is already answering")

        # Hyphens and repeated punctuation vary wildly in STT output ("mm-hm",
        # "mm hm", "Mhm..."), so flatten them before the lookup.
        stripped = " ".join(re.sub(r"[-_]+", " ", text).strip(" ?!.,").split())
        if stripped in _FILLER_UTTERANCES:
            return FloorCheck(FloorVerdict.LISTEN, "acknowledgement noise, not a turn")

        known_names = profile.names | {normalise(p) for p in context.participants if p.strip()}
        addressee = (context.addressed_to or "").strip() or self._detect_addressee(context.transcript, known_names)
        if addressee and normalise(addressee) not in profile.names and normalise(addressee) not in _DELEGATE_NAMES:
            return FloorCheck(FloorVerdict.LISTEN, f"addressed to {addressee}", addressee=addressee)

        return FloorCheck(
            FloorVerdict.RESPOND,
            "addressed to the principal" if addressee else "floor is open",
            addressee=addressee,
        )

    def check_sensitivity(self, context: MeetingContext) -> SensitivityCheck:
        """Which Representation Policy tier, if any, governs this utterance.

        Advisory rather than terminal: the planner is told what is restricted
        and writes the deflection itself, and
        :class:`~src.brain.planner.OutputGuard` re-checks the generated reply.
        Tiers restrict; they no longer decide whether the delegate may speak.

        Raises:
            ProfileNotFoundError: if ``context.user_id`` has no policy.
        """
        profile = self._store.get_profile(context.user_id)
        matches = self._classify_topics(normalise(context.transcript), profile)
        if not matches:
            return SensitivityCheck(tier=None, rule=None, triggers=())
        governing = self._most_restrictive(matches)
        return SensitivityCheck(tier=governing.rule.tier, rule=governing.rule, triggers=governing.triggers)

    # ------------------------------------------------------------------ #
    # Decision assembly
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decide(
        state: DecisionState,
        reason: str,
        trace: _Trace,
        *,
        match: TopicMatch | None = None,
        suggested_response: str | None = None,
        grounding: Grounding | None = None,
    ) -> PolicyDecision:
        """Build the terminal :class:`PolicyDecision` and close the trace."""
        trace.steps.append(TraceStep(stage=state.value, outcome=reason))
        return PolicyDecision(
            state=state,
            reason=reason,
            topic=match.rule.topic if match else None,
            tier=match.rule.tier if match else None,
            matched_triggers=list(match.triggers) if match else [],
            suggested_response=suggested_response,
            grounding=grounding,
            trace=trace.steps,
        )
