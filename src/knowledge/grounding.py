"""
Provider-agnostic grounding check for LLM-synthesised answers.

Both :class:`~src.knowledge.retriever.ClaudeAnswerSynthesiser` and
:class:`~src.knowledge.retriever.GeminiAnswerSynthesiser` ask the model to
return its answer alongside the exact sentence(s) it drew the answer from
(``citations``). :func:`verify_citations` is a cheap, purely programmatic
check - no extra LLM call - that every citation actually appears in the text
the model was given. It exists because "the model claims a citation" and
"the citation is real" are different things, and catching the gap here means
the system's reliability does not depend on how good any one model is at
staying grounded: a citation that fails this check is treated as a decline,
and the caller falls back to the extractive composer.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_QUOTE_TRANSLATION = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _normalise(text: str) -> str:
    """Collapse whitespace and straighten smart quotes, for a robust substring check.

    Deliberately does not touch case or punctuation beyond quotes: a citation
    is expected to be a verbatim quote, and normalising more than this would
    start accepting paraphrases, defeating the point of the check.
    """
    return _WHITESPACE_RE.sub(" ", text.translate(_QUOTE_TRANSLATION)).strip()


def verify_citations(citations: list[str], source_text: str) -> bool:
    """True only if every citation appears verbatim (whitespace/quote-normalised) in ``source_text``.

    An empty ``citations`` list fails closed (returns ``False``): a synthesised
    answer with no supporting citation is exactly the kind of ungrounded claim
    this check exists to catch.
    """
    if not citations:
        return False
    haystack = _normalise(source_text)
    return all(_normalise(citation) in haystack for citation in citations)
