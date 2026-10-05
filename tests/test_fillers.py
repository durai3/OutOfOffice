"""
Offline verification for context-aware, non-repeating holding phrases.

The complaint this fixes: a single flat filler list meant "let me look into
that" got said in answer to "what's your name?", and the same line got reused
several times in one meeting - both are exactly what makes a delegate sound
scripted instead of live. This checks the two mechanisms that fix it:

1. :func:`src.audio.fillers.classify_filler_style` picks a plausible style
   (quick / lookup / action / general) from the raw utterance alone, cheaply
   and without a model call.
2. :class:`src.audio.fillers.FillerPicker` avoids repeating a filler it just
   used, within a style and across many picks of the same style.

Usage::

    python tests/test_fillers.py
"""

from __future__ import annotations

import sys
from itertools import pairwise
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.audio.fillers import (  # noqa: E402
    _FILLER_BANKS,
    FillerPicker,
    FillerStyle,
    classify_filler_style,
)

GREEN, RED, BOLD, RESET = "\033[32m", "\033[31m", "\033[1m", "\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


def test_classification() -> None:
    print(f"\n{BOLD}1. filler style fits the utterance{RESET}")
    cases = [
        ("What's your name?", FillerStyle.QUICK),
        ("Are you a real person?", FillerStyle.QUICK),
        ("Thanks so much!", FillerStyle.QUICK),
        ("What is your GPA?", FillerStyle.LOOKUP),
        ("Tell me about your projects.", FillerStyle.LOOKUP),
        ("Can you recap what we've discussed?", FillerStyle.LOOKUP),
        ("Can you make sure Durai calls me back?", FillerStyle.ACTION),
        ("Please tell him to email me the slides.", FillerStyle.ACTION),
        ("You can leave the meeting now.", FillerStyle.ACTION),
        ("So, about the weather today.", FillerStyle.GENERAL),
    ]
    for text, expected in cases:
        got = classify_filler_style(text)
        check(f"{text!r} -> {expected.value}", got is expected, f"got {got.value}")


def test_no_immediate_repeat() -> None:
    print(f"\n{BOLD}2. the picker avoids repeating a filler it just used{RESET}")
    picker = FillerPicker(memory=4)
    picks = [picker.pick("What is your GPA?") for _ in range(20)]
    repeats = sum(1 for a, b in pairwise(picks) if a == b)
    check("no two consecutive picks are identical over 20 draws", repeats == 0, f"{repeats} repeats")

    bank_size = len(_FILLER_BANKS[FillerStyle.LOOKUP])
    check("the lookup bank has real variety to draw from", bank_size >= 4, f"bank size {bank_size}")
    distinct = len(set(picks))
    check("more than one distinct phrase was actually used", distinct > 1, f"only {distinct} distinct phrase(s)")


def test_every_style_has_a_bank() -> None:
    print(f"\n{BOLD}3. every style has its own non-empty bank{RESET}")
    for style in FillerStyle:
        bank = _FILLER_BANKS.get(style, ())
        check(f"{style.value} has fillers", len(bank) >= 3, f"{len(bank)} entries")


def test_quick_style_has_no_hesitation() -> None:
    print(f"\n{BOLD}4. quick acknowledgements do not hesitate{RESET}")
    quick_bank = _FILLER_BANKS[FillerStyle.QUICK]
    hesitant = [p for p in quick_bank if p.lower().startswith(("hmm", "uh", "let me think"))]
    check("no hesitation sounds in the quick bank (a human doesn't stall before 'sure')", not hesitant, str(hesitant))


def main() -> int:
    test_classification()
    test_no_immediate_repeat()
    test_every_style_has_a_bank()
    test_quick_style_has_no_hesitation()
    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + ", ".join(_failures))
        return 1
    print(f"{GREEN}{BOLD}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
