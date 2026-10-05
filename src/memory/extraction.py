"""
Rule-based extraction for the live meeting state.

Everything in this module is deterministic and runs in microseconds so the
meeting state can be updated on every utterance without touching a model.
Three questions are answered:

* :func:`extract_decision`  - did the group just agree to something?
* :func:`extract_actions`   - did someone take (or get given) a task, and by when?
* :func:`infer_topic`       - what is the conversation about right now?

Relative deadlines ("by next Tuesday", "by end of week") are resolved to
calendar dates from the utterance timestamp by :func:`resolve_deadline`.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_WEEKDAYS: dict[str, int] = {
    name.lower(): index for index, name in enumerate(calendar.day_name)
} | {name.lower(): index for index, name in enumerate(calendar.day_abbr)}
_MONTHS: dict[str, int] = {
    name.lower(): index for index, name in enumerate(calendar.month_name) if name
} | {name.lower(): index for index, name in enumerate(calendar.month_abbr) if name}

_MONTH_ALT = "|".join(sorted({m for m in _MONTHS if len(m) == 3}))
_WEEKDAY_ALT = "|".join(sorted(calendar.day_name, key=len, reverse=True)).lower()

_STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been being but by can could did do does doing for from get got had
    has have he her hers him his how i if in into is it its just let let's lets me my of on or our
    ours out she so some than that the their theirs them then there these they this those to too
    up us very was we were what when where which who whom why will with would you your yours
    yes no ok okay great good fine well right sure thanks thank please really also still already
    next last this coming end start before after until due by agree agreed agreeing finish
    finished done make sure need needs going go move forward ahead should must have""".split()
)
_TIME_WORDS: frozenset[str] = frozenset(
    set(_WEEKDAYS) | set(_MONTHS) | {"today", "tomorrow", "tonight", "week", "month", "quarter", "year", "eod", "eow", "eom"}
)

# --------------------------------------------------------------------------- #
# Deadlines
# --------------------------------------------------------------------------- #

_DEADLINE_RE = re.compile(
    r"\b(?P<lead>by|before|until|due|no later than|on)\s+"
    r"(?P<phrase>"
    r"(?:the\s+)?end\s+of\s+(?:the\s+|this\s+|next\s+)?(?:day|week|month|quarter|year)"
    r"|eod|eow|eom|eoy|cob"
    r"|today|tomorrow|tonight"
    r"|(?:this|next)\s+(?:week|month|quarter|year)"
    r"|(?:(?:this|next|coming)\s+)?(?:" + _WEEKDAY_ALT + r")"
    r"|q[1-4](?:\s+\d{4})?"
    r"|(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?(?:\s+of)?\s+(?:" + _MONTH_ALT + r")[a-z]*(?:,?\s+\d{4})?"
    r"|(?:" + _MONTH_ALT + r")[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|the\s+\d{1,2}(?:st|nd|rd|th)"
    r")(?![a-z])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Deadline:
    """A due-date phrase as spoken, plus the calendar date it resolves to."""

    text: str
    """The phrase including its lead word, e.g. ``"by next Tuesday"``."""
    due: date | None
    """Resolved date, or ``None`` when the phrase could not be interpreted."""

    @property
    def label(self) -> str:
        """Human-readable form: the resolved date when known, else the phrase."""
        return self.due.strftime("%a %d %b %Y") if self.due else self.text


def _end_of_month(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    return date(year, month, calendar.monthrange(year, month)[1])


def _next_weekday(today: date, weekday: int, *, following_week: bool) -> date:
    if following_week:
        days_to_monday = (7 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_to_monday + weekday)
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta)


def resolve_deadline(phrase: str, now: datetime) -> date | None:
    """Turn a spoken deadline phrase into a date relative to ``now``.

    Conventions: "next week/month" resolve to the start of that period;
    "end of week" is Friday; a bare weekday is its next occurrence (today if
    it is that day); "next <weekday>" is that day in the following week.
    """
    today = now.date()
    text = " ".join(phrase.lower().replace(",", " ").split())
    text = re.sub(r"^(?:by|before|until|due|no later than|on)\s+", "", text)
    text = re.sub(r"^the\s+", "", text)

    if text in {"today", "tonight", "eod", "cob", "end of day", "end of the day"}:
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text in {"eow", "end of week", "end of the week", "end of this week", "this week"}:
        return _next_weekday(today, 4, following_week=today.weekday() > 4)
    if text == "end of next week":
        return _next_weekday(today, 4, following_week=True)
    if text in {"eom", "end of month", "end of the month", "end of this month", "this month"}:
        return _end_of_month(today)
    if text == "end of next month":
        first_next = (_end_of_month(today) + timedelta(days=1))
        return _end_of_month(first_next)
    if text in {"eoy", "end of year", "end of the year", "end of this year", "this year"}:
        return date(today.year, 12, 31)
    if text in {"end of quarter", "end of the quarter", "end of this quarter", "this quarter"}:
        return _quarter_end(today.year, (today.month - 1) // 3 + 1)
    if text == "end of next quarter":
        quarter = (today.month - 1) // 3 + 2
        return _quarter_end(today.year + (quarter > 4), (quarter - 1) % 4 + 1)
    if text == "next week":
        return _next_weekday(today, 0, following_week=True)
    if text == "next month":
        return _end_of_month(today) + timedelta(days=1)
    if text == "next quarter":
        quarter = (today.month - 1) // 3 + 2
        year = today.year + (quarter > 4)
        return date(year, ((quarter - 1) % 4) * 3 + 1, 1)
    if text == "next year":
        return date(today.year + 1, 1, 1)

    match = re.fullmatch(r"(?:(this|next|coming)\s+)?(" + _WEEKDAY_ALT + r")", text)
    if match:
        qualifier, day_name = match.group(1), match.group(2)
        return _next_weekday(today, _WEEKDAYS[day_name], following_week=qualifier == "next")

    match = re.fullmatch(r"q([1-4])(?:\s+(\d{4}))?", text)
    if match:
        return _quarter_end(int(match.group(2) or today.year), int(match.group(1)))

    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+([a-z]+)(?:\s+(\d{4}))?", text)
    if not match:
        swapped = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?", text)
        if swapped:
            match = re.fullmatch(
                r"(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?", f"{swapped.group(2)} {swapped.group(1)}" + (f" {swapped.group(3)}" if swapped.group(3) else "")
            )
    if match:
        day_number, month_name, year_text = int(match.group(1)), match.group(2), match.group(3)
        month = _MONTHS.get(month_name) or _MONTHS.get(month_name[:3])
        if month is None:
            return None
        year = int(year_text) if year_text else today.year
        try:
            candidate = date(year, month, day_number)
        except ValueError:
            return None
        if not year_text and candidate < today:
            candidate = date(year + 1, month, day_number)
        return candidate

    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)", text)
    if match:
        day_number = int(match.group(1))
        for candidate_month in (today, _end_of_month(today) + timedelta(days=1)):
            try:
                candidate = candidate_month.replace(day=day_number)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
    return None


def find_deadline(text: str, now: datetime) -> Deadline | None:
    """Locate the first deadline phrase in ``text`` and resolve it."""
    match = _DEADLINE_RE.search(text)
    if not match:
        return None
    phrase = f"{match.group('lead')} {match.group('phrase')}"
    return Deadline(text=" ".join(phrase.split()), due=resolve_deadline(match.group("phrase"), now))


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #

_INTERJECTION_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|great|good|fine|alright|right|so|well|yes|yeah|yep|perfect|sounds good|thanks|thank you|cool|sure|then)[,.!\s]+)+",
    re.IGNORECASE,
)

_DECISION_LEADS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\blet'?s agree (?:to|on|that|with)\s+",
        r"\b(?:we|i) (?:all |both )?agree (?:to|on|that|with)\s+",
        r"\bwe(?:'ve| have) (?:all )?agreed (?:to|on|that)\s+",
        r"\b(?:it'?s|it is) (?:agreed|settled|decided) (?:that\s+|to\s+)?",
        r"^agreed[,:.!]?\s+(?:we(?:'ll| will)\s+)?",
        r"\blet'?s (?:go|move forward|move ahead|proceed) with\s+",
        r"\bwe(?:'ll| will) (?:go|move forward|move ahead|proceed) with\s+",
        r"\b(?:the |our )?decision is (?:to\s+|that\s+)?",
        r"\bwe(?:'ve| have)? decided (?:to|on|that)\s+",
        r"\blet'?s (?:finali[sz]e|lock in|settle on|commit to|confirm)\s+",
        r"\bwe(?:'re| are) aligned (?:on|that)\s+",
        r"\bconsider it (?:done|agreed|settled)[,:.]?\s*",
        r"\bfinal (?:answer|decision|call)(?: is)?[:,]?\s*",
    )
)

_TRAILING_PUNCT_RE = re.compile(r"[\s.!?,;:]+$")


def _clean_statement(text: str) -> str:
    text = _TRAILING_PUNCT_RE.sub("", text.strip())
    text = re.sub(r"^(?:that|to)\s+", "", text, flags=re.IGNORECASE)
    return text[:1].upper() + text[1:] if text else text


def strip_interjections(text: str) -> str:
    """Drop leading filler such as "Great, " or "Okay so "."""
    return _INTERJECTION_RE.sub("", text.strip())


def extract_decision(text: str) -> str | None:
    """Return the agreed statement when ``text`` records an agreement, else ``None``."""
    body = strip_interjections(text)
    for lead in _DECISION_LEADS:
        match = lead.search(body)
        if match:
            statement = _clean_statement(body[match.end():])
            if len(statement.split()) >= 2:
                return statement
    return None


# --------------------------------------------------------------------------- #
# Action items
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExtractedAction:
    """A task someone committed to, with its owner and (optional) deadline."""

    description: str
    owner: str
    owner_inferred: bool
    """True when the owner was not named explicitly (e.g. "we'll ..." or "let's ...")."""
    deadline: Deadline | None


_NAME = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"

_OWNED_ACTION_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\b(?P<owner>I|We|{_NAME})(?:'ll| will| can| shall| am going to| is going to| are going to| need(?:s)? to| has to| have to| should) (?:also |then |just )?(?P<task>.+)",
        rf"\b(?P<owner>{_NAME}) (?:owns|will own|to own|is responsible for|is going to own|takes|will take|takes care of|will take care of|handles|will handle|to handle|will drive|is driving|will follow up on|will send|will share|will prepare|will draft|will review) (?P<task>.+)",
        rf"\b[Aa]ction [Ii]tem(?: for (?P<owner>{_NAME}))?[:\-]?\s*(?P<task>.+)",
        rf"^(?P<owner>{_NAME})[,:]\s*(?:can|could|would|will) you (?:please )?(?P<task>.+)",
        rf"\b(?:assign|assigning|assigned|hand|handing) (?:this|that|it)?\s*(?:over )?to (?P<owner>{_NAME})\b(?P<task>.*)",
    )
)

_GROUP_ACTION_RE = re.compile(
    r"\b(?:let'?s(?: all)?(?: agree to| make sure (?:we|to)| plan to)?|we need to|we should|we have to|we must|we ought to|make sure (?:we|to)|we(?:'ll| will)(?: need to)?)\s+(?P<task>.+)",
    re.IGNORECASE,
)

_TASK_STRIP_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\s*\bplease\b\s*", re.IGNORECASE),
    re.compile(r"^(?:to|that we|that)\s+", re.IGNORECASE),
    re.compile(r"^(?:get|go)\s+(?:this|that|it)\s+", re.IGNORECASE),
)

_NOT_A_TASK: frozenset[str] = frozenset(
    {"agree", "agreed", "be", "see", "say", "think", "know", "talk", "discuss", "move", "go", "do", "have"}
)


def _resolve_owner(token: str | None, speaker: str, addressee: str | None, participants: list[str]) -> tuple[str, bool]:
    """Map an owner token to a participant name. Returns ``(owner, inferred)``."""
    if token is None:
        return speaker, True
    key = token.strip()
    lowered = key.lower()
    if lowered in {"i", "i'll", "me"}:
        return speaker, False
    if lowered in {"we", "us"}:
        return speaker, True
    if lowered == "you":
        return (addressee or speaker), True
    for participant in participants:
        if participant.lower() == lowered or participant.lower().split()[0] == lowered:
            return participant, False
    return key, False


def _clean_task(task: str, deadline: Deadline | None) -> str:
    text = task.strip()
    if deadline is not None:
        text = re.sub(re.escape(deadline.text), " ", text, flags=re.IGNORECASE)
    for pattern in _TASK_STRIP_RES:
        text = pattern.sub(" ", text)
    text = " ".join(text.split())
    text = _TRAILING_PUNCT_RE.sub("", text)
    return text[:1].upper() + text[1:] if text else text


def _is_meaningful(task: str) -> bool:
    words = task.lower().split()
    return len(words) >= 2 and words[0] not in _NOT_A_TASK


def extract_actions(
    text: str,
    *,
    speaker: str,
    addressee: str | None,
    participants: list[str],
    now: datetime,
) -> list[ExtractedAction]:
    """Return the action items an utterance creates (usually zero or one).

    An explicit owner ("Sarah will ...", "I'll ...", "Mike, can you ...") is
    always an action. A group commitment ("let's ...", "we need to ...") only
    counts when it also carries a deadline or an agreement marker, so idle
    suggestions do not pollute the list.
    """
    body = strip_interjections(text)
    deadline = find_deadline(body, now)
    actions: list[ExtractedAction] = []

    for pattern in _OWNED_ACTION_RES:
        match = pattern.search(body)
        if not match:
            continue
        task = _clean_task(match.group("task") or "", deadline)
        if not _is_meaningful(task):
            continue
        owner, inferred = _resolve_owner(match.group("owner"), speaker, addressee, participants)
        if pattern.pattern.startswith("^(?P<owner>") and addressee is None:
            inferred = True  # a request to someone is a task only once they accept
        actions.append(ExtractedAction(description=task, owner=owner, owner_inferred=inferred, deadline=deadline))
        break

    if not actions:
        group = _GROUP_ACTION_RE.search(body)
        if group and (deadline is not None or extract_decision(body) is not None):
            task = _clean_task(group.group("task"), deadline)
            if _is_meaningful(task):
                actions.append(ExtractedAction(description=task, owner=speaker, owner_inferred=True, deadline=deadline))
    return actions


# --------------------------------------------------------------------------- #
# Topic inference
# --------------------------------------------------------------------------- #

_PROPER_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){0,2})\b")
_WORD_RE = re.compile(r"[a-z][a-z0-9'\-]+")


def _title(label: str) -> str:
    return label[:1].upper() + label[1:]


def _proper_phrases(text: str, exclude: set[str]) -> list[str]:
    phrases: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        for match in _PROPER_PHRASE_RE.finditer(sentence):
            phrase = match.group(1)
            if match.start() == 0:
                continue  # sentence-initial capital carries no signal
            words = phrase.lower().split()
            if any(w in exclude or w in _TIME_WORDS or w in _STOPWORDS for w in words):
                continue
            phrases.append(phrase)
    return phrases


def _content_bigrams(text: str) -> list[str]:
    tokens = _WORD_RE.findall(text.lower())
    bigrams: list[str] = []
    for left, right in zip(tokens, tokens[1:]):
        if left in _STOPWORDS or right in _STOPWORDS or left in _TIME_WORDS or right in _TIME_WORDS:
            continue
        bigrams.append(f"{left} {right}")
    return bigrams


def infer_topic(
    recent_texts: list[str],
    *,
    policy_topic: str | None,
    grounding_subject: str | None,
    exclude_names: set[str],
) -> str | None:
    """Pick a short label for what the conversation is about right now.

    Precedence: the document subject that grounded an answer; the policy topic
    the brain classified; a proper-noun phrase in the latest utterance; a
    content bigram ("security review"); ``None`` when nothing stands out.
    """
    if grounding_subject:
        return _title(grounding_subject)
    if policy_topic and policy_topic != "documented_facts":
        return _title(policy_topic.replace("_", " "))
    excluded = {n.lower() for n in exclude_names} | {part for n in exclude_names for part in n.lower().split()}
    for text in reversed(recent_texts):
        phrases = _proper_phrases(text, excluded)
        if phrases:
            return max(phrases, key=lambda p: (phrases.count(p), len(p)))
        bigrams = _content_bigrams(text)
        if bigrams:
            return _title(max(bigrams, key=lambda b: (bigrams.count(b), len(b))))
    return None


def leading_addressee(text: str, participants: list[str]) -> str | None:
    """Return the participant named at the start of ``text`` ("Mike, can you ...")."""
    match = re.match(r"^\W*(?:(?:hey|hi|hello|ok|okay)[\s,]+)?(?P<name>[A-Za-z][A-Za-z'\-]*)\s*[,:;\-]", text)
    if not match:
        return None
    key = match.group("name").lower()
    for participant in participants:
        if participant.lower() == key or participant.lower().split()[0] == key:
            return participant
    return None
