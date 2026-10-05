"""
Context-aware holding phrases for the thinking-time gap.

A flat list of generic fillers reused for every kind of question is what makes
a delegate sound like a bot: it says "let me look into that" in answer to
"what's your name?", and it says the exact same line twice within one meeting.
This module picks a filler that actually fits the shape of the question - a
quick social remark gets a quick acknowledgement, a factual lookup gets a
"hmm, let me think" - and avoids repeating one of the last few fillers used.

The classification here is a cheap keyword heuristic, deliberately not a model
call: it only shapes the tone of a half-second holding phrase, so getting it
slightly wrong just means a mildly mismatched filler, never a wrong answer -
unlike everything the actual response planner decides.
"""

from __future__ import annotations

import random
from collections import deque
from enum import Enum

from src.brain.text import matched_phrases, normalise


class FillerStyle(str, Enum):
    """The shape of thinking-out-loud that fits an utterance."""

    QUICK = "quick"
    """Social, identity, or acknowledgement - answered instantly, no real recall."""
    LOOKUP = "lookup"
    """A factual question that genuinely means pausing to recall or check something."""
    ACTION = "action"
    """A request, commitment, or leave-the-call ask - a "let me note that" moment."""
    GENERAL = "general"
    """Could not be guessed confidently; a neutral holding phrase."""


_QUICK_MARKERS: tuple[str, ...] = (
    "your name", "who are you", "are you a", "are you real", "are you human",
    "can you hear", "how are you", "how's it going", "how is it going",
    "what can you help", "nice to meet", "good morning", "good afternoon",
    "good evening", "thank you", "thanks", "how are things", "hello", "hi there",
)
_ACTION_MARKERS: tuple[str, ...] = (
    "make sure", "tell him", "tell her", "let him know", "let her know",
    "pass this on", "pass that on", "call me", "email me", "message me",
    "text me", "follow up", "can you ask", "can you tell", "reach out",
    "leave the meeting", "end the call", "hang up", "disconnect",
)
_LOOKUP_MARKERS: tuple[str, ...] = (
    "what is", "what's", "what are", "tell me about", "tell us about",
    "how much", "how many", "when did", "when was", "where did", "which",
    "gpa", "university", "college", "school", "project", "experience",
    "worked on", "applied", "internship", "company", "recap", "summar",
)

_FILLER_BANKS: dict[FillerStyle, tuple[str, ...]] = {
    FillerStyle.QUICK: (
        "Sure.",
        "Yeah, sure.",
        "Oh, sure thing.",
        "Mhm.",
        "Of course.",
        "Right, sure.",
    ),
    FillerStyle.LOOKUP: (
        "Hmm, let me think.",
        "Let me pull that up.",
        "One sec, let me check.",
        "Let's see here.",
        "Give me just a moment.",
        "Uh, one moment - let me think.",
        "Hold on, let me recall that.",
    ),
    FillerStyle.ACTION: (
        "Got it, one sec.",
        "Okay, let me get that down.",
        "Sure, hold on a moment.",
        "Right, noted - one sec.",
        "Mm, got that, one moment.",
    ),
    FillerStyle.GENERAL: (
        "One moment.",
        "Just a sec.",
        "Hmm, one moment.",
        "Let me think for a sec.",
        "Hold on a moment.",
    ),
}


def classify_filler_style(transcript: str) -> FillerStyle:
    """Guess the shape of thinking-out-loud that fits ``transcript``.

    Order matters: a quick social/identity question ("What's your name?") also
    ends in "?" and would otherwise fall into LOOKUP, so QUICK is checked first;
    likewise a request to act ("Can you make sure...") can also read as a
    question, so ACTION is checked before the generic LOOKUP/"?" fallback.
    """
    text = normalise(transcript)
    if matched_phrases(text, _QUICK_MARKERS):
        return FillerStyle.QUICK
    if matched_phrases(text, _ACTION_MARKERS):
        return FillerStyle.ACTION
    if matched_phrases(text, _LOOKUP_MARKERS) or text.endswith("?"):
        return FillerStyle.LOOKUP
    return FillerStyle.GENERAL


class FillerPicker:
    """Picks a filler for one utterance, avoiding recent repeats.

    State is scoped to one voice session (one meeting): a delegate that says
    "Let me check that." for the third time in ten minutes sounds scripted, so
    the last few fillers spoken are remembered and excluded from the next pick
    wherever the bank is large enough to still offer a choice.
    """

    def __init__(self, *, memory: int = 4) -> None:
        """The last ``memory`` fillers spoken (across all styles) are avoided when possible."""
        self._recent: deque[str] = deque(maxlen=memory)

    def pick(self, transcript: str) -> str:
        """A filler phrase that fits ``transcript``, preferring one not said recently."""
        bank = _FILLER_BANKS[classify_filler_style(transcript)]
        fresh = [phrase for phrase in bank if phrase not in self._recent]
        choice = random.choice(fresh or list(bank))
        self._recent.append(choice)
        return choice


__all__ = ["FillerPicker", "FillerStyle", "classify_filler_style"]
