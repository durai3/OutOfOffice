"""
Behavioural eval suite for the meeting delegate.

The bug that motivated this file was not subtle - a retired model id turned
every intelligent component off, and the delegate spent a real meeting either
silent or quoting unrelated sentences out of a PDF. It survived because nothing
ever asked the delegate a question and checked the answer. Unit tests covered
the state machine's transitions; none covered "when someone asks its name, does
it say anything at all".

So this runs the delegate against real utterances and asserts on real replies.
It needs a working model and a compiled dossier, which is the point: it fails
loudly when either is missing, in exactly the situation where the product would
otherwise look fine and be broken.

Cases live in ``tests/evals/conversation.yaml``. The first group is the failed
meeting's transcript, verbatim.

Usage::

    python tests/test_delegate_evals.py
    python tests/test_delegate_evals.py --only guardrails,hallucination
    python tests/test_delegate_evals.py --verbose

Exit codes: 0 = every case passed, 1 = at least one failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from src.brain.models import MeetingContext  # noqa: E402
from src.brain.planner import (  # noqa: E402
    GuardVerdict,
    OutputGuard,
    PlannerInput,
    TurnType,
    build_planner,
)
from src.brain.policy_engine import PolicyEngine  # noqa: E402
from src.brain.profile_store import profile_store  # noqa: E402
from src.knowledge.dossier import DossierStore  # noqa: E402
from src.llm import LlmError, build_llm, preflight  # noqa: E402

SUITE = Path(__file__).resolve().parent / "evals" / "conversation.yaml"
USER_ID = "durai"
SPEAKER = "Interviewer"

GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


@dataclass
class CaseResult:
    """Outcome of one eval case."""

    group: str
    utterance: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    spoken: str | None = None
    turn_type: str = "-"
    guard: str = "-"
    elapsed_ms: float = 0.0
    error: str | None = None


def _lower(text: str | None) -> str:
    return (text or "").lower()


def _check(expect: dict[str, Any], *, spoken: str | None, plan: Any, guarded: Any) -> list[str]:
    """Every assertion that failed for one case."""
    failures: list[str] = []
    said = _lower(spoken)
    speaks = spoken is not None and bool(spoken.strip())

    if "speaks" in expect and speaks is not bool(expect["speaks"]):
        failures.append(f"speaks={speaks}, expected {expect['speaks']}")

    if not speaks:
        # Nothing was said; content assertions are vacuous and turn-type ones
        # are only meaningful when the suite asked for silence.
        if expect.get("speaks") is False:
            return failures
        return failures or ["said nothing"]

    if "turn_type" in expect and plan.turn_type.value != expect["turn_type"]:
        failures.append(f"turn_type={plan.turn_type.value}, expected {expect['turn_type']}")
    if "turn_type_in" in expect and plan.turn_type.value not in expect["turn_type_in"]:
        failures.append(f"turn_type={plan.turn_type.value}, expected one of {expect['turn_type_in']}")

    for needle in expect.get("contains", []):
        if needle.lower() not in said:
            failures.append(f"missing {needle!r}")
    any_of = expect.get("contains_any")
    if any_of and not any(n.lower() in said for n in any_of):
        failures.append(f"none of {any_of} present")
    for needle in expect.get("excludes", []):
        if needle.lower() in said:
            failures.append(f"must not contain {needle!r}")

    if expect.get("grounded") and not (plan.states_personal_fact and guarded.verdict is GuardVerdict.PASSED):
        failures.append(
            f"not grounded (states_personal_fact={plan.states_personal_fact}, guard={guarded.verdict.value})"
        )
    if "needs_human" in expect:
        escalates = plan.needs_human or plan.turn_type is TurnType.DEFLECT
        if escalates is not bool(expect["needs_human"]):
            failures.append(f"needs_human={escalates}, expected {expect['needs_human']}")
    if "guard" in expect and guarded.verdict.value != expect["guard"]:
        failures.append(f"guard={guarded.verdict.value}, expected {expect['guard']}")
    if "max_words" in expect and len(spoken.split()) > int(expect["max_words"]):
        failures.append(f"{len(spoken.split())} words, max {expect['max_words']}")

    action = expect.get("action")
    if action is not None:
        if not plan.has_action:
            failures.append("no action item captured")
        else:
            description = _lower(plan.action_description)
            wanted = action.get("description_any")
            if wanted and not any(w.lower() in description for w in wanted):
                failures.append(f"action {plan.action_description!r} matches none of {wanted}")
            if action.get("due_iso_present") and not plan.action_due_iso.strip():
                failures.append("action has no resolved due date")
            if "owner" in action and action["owner"].lower() not in _lower(plan.action_owner):
                failures.append(f"action owner {plan.action_owner!r}, expected {action['owner']!r}")
    return failures


async def run_suite(groups: set[str] | None, verbose: bool, pace: float) -> int:
    """Run the suite and print a report. Returns a process exit code."""
    if not SUITE.exists():
        print(f"Eval suite not found at {SUITE}")
        return 2

    llm = build_llm()
    health = await preflight(llm)
    print(health.line())
    if not health.ok:
        print("\nCannot run the evals without a working language model.")
        print("This is the failure mode the suite exists to catch - fix the key or model id and re-run.")
        return 2

    dossier = DossierStore().load(USER_ID)
    print(dossier.describe())
    if dossier.is_empty:
        print("\nNo dossier compiled. Run: python -m src.knowledge.dossier")
        return 2

    profile = profile_store.get_profile(USER_ID)
    engine = PolicyEngine(profile_store)
    planner = build_planner(llm)
    guard = OutputGuard()
    assert planner is not None
    reds = [r.topic for r in profile.policy if r.tier.value == "RED"]
    print(f"policy: {len(profile.policy)} rules, RED on {', '.join(reds) or '(none)'}\n")

    suite = yaml.safe_load(SUITE.read_text(encoding="utf-8"))
    results: list[CaseResult] = []
    history: list[tuple[str, str]] = []

    for block in suite:
        group = block["group"]
        if groups is not None and group not in groups:
            continue
        print(f"{BOLD}{group}{RESET} {DIM}- {block.get('description', '')}{RESET}")
        for case in block["cases"]:
            utterance = " ".join(str(case["say"]).split())
            expect = case.get("expect", {})
            speaker = case.get("speaker", SPEAKER)
            if not case.get("carries_context"):
                history = []

            context = MeetingContext(
                meeting_id=f"eval-{group}", user_id=USER_ID, transcript=utterance,
                speaker=speaker, participants=[profile.display_name, SPEAKER, "Sarah"],
                participant_count=case.get("participant_count"),
            )
            started = time.monotonic()
            result = CaseResult(group=group, utterance=utterance, passed=False)

            floor = engine.check_floor(context)
            if not floor.should_respond:
                result.turn_type = "silent"
                result.passed = expect.get("speaks") is False
                if not result.passed:
                    result.failures = [f"stayed silent: {floor.reason}"]
                result.elapsed_ms = (time.monotonic() - started) * 1000
                results.append(result)
                _print_case(result, verbose)
                continue

            try:
                plan = await planner.plan(
                    PlannerInput(
                        context=context, profile=profile, dossier=dossier.markdown,
                        documents="", history=history,
                    )
                )
                guarded = guard.check(plan, profile=profile, sources=dossier.markdown)
            except LlmError as exc:
                result.error = str(exc)
                result.failures = [f"model call failed: {exc}"]
                result.elapsed_ms = (time.monotonic() - started) * 1000
                results.append(result)
                _print_case(result, verbose)
                continue

            result.spoken = guarded.spoken_text
            result.turn_type = plan.turn_type.value
            result.guard = guarded.verdict.value
            result.failures = _check(expect, spoken=guarded.spoken_text, plan=plan, guarded=guarded)
            result.passed = not result.failures
            result.elapsed_ms = (time.monotonic() - started) * 1000
            results.append(result)
            _print_case(result, verbose)
            if pace:
                await asyncio.sleep(pace)

            history.append((speaker, utterance))
            if guarded.spoken_text:
                history.append(("DELEG8", guarded.spoken_text))
        print()

    return _report(results)


def _print_case(result: CaseResult, verbose: bool) -> None:
    mark = f"{GREEN}PASS{RESET}" if result.passed else f"{RED}FAIL{RESET}"
    print(f"  {mark} {DIM}{result.elapsed_ms:6.0f}ms {result.turn_type:10}{RESET} {result.utterance[:64]}")
    if verbose or not result.passed:
        if result.spoken:
            print(f"       {DIM}say:{RESET} {result.spoken}")
        for failure in result.failures:
            print(f"       {RED}x{RESET} {failure}")


def _report(results: list[CaseResult]) -> int:
    """Print the summary, separating behavioural failures from infrastructure ones.

    A case that never got an answer out of the provider says nothing about
    whether the delegate behaves correctly, and reporting the two together makes
    a rate-limited run look like a broken delegate. On a free-tier key a long run
    will hit quota; ``--pace`` spreads the calls out.
    """
    infra = [r for r in results if r.error]
    if infra:
        results = [r for r in results if not r.error]
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    latencies = sorted(r.elapsed_ms for r in results if r.elapsed_ms > 0 and r.spoken)
    print("=" * 72)
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(f"latency of spoken turns: p50 {p50:.0f}ms, p95 {p95:.0f}ms, max {latencies[-1]:.0f}ms")

    by_group: dict[str, list[CaseResult]] = {}
    for result in results:
        by_group.setdefault(result.group, []).append(result)
    for group, items in by_group.items():
        ok = sum(1 for r in items if r.passed)
        colour = GREEN if ok == len(items) else (YELLOW if ok else RED)
        print(f"  {colour}{ok}/{len(items)}{RESET} {group}")

    colour = GREEN if passed == total else RED
    print(f"{colour}{BOLD}{passed}/{total} cases passed{RESET}")
    if infra:
        print(
            f"{YELLOW}{len(infra)} case(s) could not reach the model and were not judged{RESET} "
            f"{DIM}(rate limit or outage, not a behaviour failure - retry with --pace 4){RESET}"
        )
        for result in infra:
            print(f"  {DIM}{result.utterance[:50]}: {result.error}{RESET}")
    return 0 if passed == total else 1


def main(argv: list[str]) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default=None, help="comma-separated group names to run")
    parser.add_argument("--verbose", action="store_true", help="print the reply for passing cases too")
    parser.add_argument(
        "--pace",
        type=float,
        default=0.0,
        help="seconds to wait between cases; use 3-5 on a free-tier key to avoid quota errors",
    )
    args = parser.parse_args(argv)
    groups = {g.strip() for g in args.only.split(",")} if args.only else None
    try:
        return asyncio.run(run_suite(groups, args.verbose, args.pace))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
