"""
Text normalisation and phrase matching shared by the brain and the memory engine.

Everything here is pure and cheap: lower-casing, whitespace collapsing, and
whole-phrase matching that never fires inside a longer word.
"""

from __future__ import annotations

import re
from functools import lru_cache


def normalise(text: str) -> str:
    """Lower-case, straighten curly quotes, and collapse whitespace for matching."""
    straightened = text.replace("’", "'").replace("‘", "'")
    return " ".join(straightened.lower().split())


@lru_cache(maxsize=2048)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile a whole-phrase matcher that will not fire inside a longer word."""
    prefix = r"(?<![a-z0-9])" if phrase[:1].isalnum() else ""
    suffix = r"(?![a-z0-9])" if phrase[-1:].isalnum() else ""
    return re.compile(prefix + re.escape(phrase) + suffix)


def contains_phrase(text: str, phrase: str) -> bool:
    """True when ``phrase`` occurs in normalised ``text`` as a whole word or phrase."""
    return phrase_pattern(phrase).search(text) is not None


def matched_phrases(text: str, phrases: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return every phrase from ``phrases`` present in normalised ``text``."""
    return tuple(phrase for phrase in phrases if contains_phrase(text, phrase))


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences on terminal punctuation and line breaks."""
    sentences: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        sentences.extend(part.strip() for part in _SENTENCE_BOUNDARY.split(line) if part.strip())
    return sentences
