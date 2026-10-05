"""
Standalone verification for Sprint 1 of the DELEG8 Policy Router Brain.

Sends three simulated meeting prompts to a *running* FastAPI server and
checks the routing state each one produces. Uses only the standard
library for HTTP so nothing beyond ``requirements.txt`` is needed.

Since DELEG8 became multi-tenant the brain runs against *a* principal rather
than *the* principal, so this creates a throwaway account (with the knowledge
the GREEN scenario needs) and evaluates against that. The account is deleted
when the run passes.

Run the server first, then either::

    python tests/test_brain.py          # human-readable report, non-zero exit on failure
    python -m pytest tests/test_brain.py  # if pytest happens to be installed

The target server is read from ``DELEG8_BASE_URL`` (default
``http://127.0.0.1:8000``); a ``.env`` file in the project root is honoured.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import ApiError, TestClient, server_is_up, test_account  # noqa: E402

load_dotenv()

BASE_URL: str = os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT_SECONDS: float = 5.0

MEETING_ID: str = "sprint1-verification"
PRINCIPAL_NAME: str = "Dana Verifier"
PARTICIPANTS: list[str] = [PRINCIPAL_NAME, "Sarah", "Mike"]

STATUS_FACT: str = (
    "# API integration status\n\n"
    "The API integration is on track. Authentication and webhook handlers are "
    "complete, rate limiting is in code review, and integration testing is "
    "scheduled for this sprint.\n"
)
"""The GREEN scenario asks a question the delegate must be able to answer. It
used to come from a hard-coded seed profile; now it is a document this test's
own account uploads, which is also a more honest test of the real path."""


@dataclass(frozen=True)
class Scenario:
    """One simulated utterance and the routing state it must produce."""

    name: str
    transcript: str
    speaker: str
    expected_state: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="GREEN status question",
        transcript="What is the status of the API integration?",
        speaker="Mike",
        expected_state="AUTO_REPLY",
    ),
    Scenario(
        name="RED pricing request",
        transcript="Can we lower the enterprise price by 20%?",
        speaker="Mike",
        expected_state="ESCALATE",
    ),
    Scenario(
        name="Social chatter to someone else",
        transcript="Hey Sarah, how was your weekend?",
        speaker="Mike",
        expected_state="SILENT",
    ),
)


def evaluate(client: TestClient, scenario: Scenario) -> dict[str, Any]:
    """Send one scenario to ``/api/v1/evaluate`` as ``client`` and return the PolicyDecision."""
    return client.post(
        "/api/v1/evaluate",
        {
            "meeting_id": MEETING_ID,
            "user_id": client.principal,
            "transcript": scenario.transcript,
            "speaker": scenario.speaker,
            "participants": [client.display_name, "Sarah", "Mike"],
        },
    )


def ensure_server_is_up() -> None:
    """Exit with a clear message if the API is not reachable."""
    health = server_is_up()
    if health.get("status") not in ("ok", "degraded"):
        sys.exit(f"DELEG8 health check failed: {health}")


# --------------------------------------------------------------------------- #
# pytest-compatible assertions (also used by the standalone runner)
# --------------------------------------------------------------------------- #

def _assert_scenario(client: TestClient, scenario: Scenario) -> dict[str, Any]:
    decision = evaluate(client, scenario)
    assert decision["state"] == scenario.expected_state, (
        f"{scenario.name}: expected {scenario.expected_state}, got {decision['state']} "
        f"(reason: {decision.get('reason')})"
    )
    assert decision["reason"], f"{scenario.name}: reason must not be empty"
    assert decision["requires_human"] is (scenario.expected_state == "ESCALATE")
    if scenario.expected_state == "AUTO_REPLY":
        assert decision["suggested_response"], f"{scenario.name}: AUTO_REPLY must carry a response"
    else:
        assert decision["suggested_response"] is None
    return decision


def _client() -> TestClient:
    """A throwaway account with the document the GREEN scenario needs."""
    ensure_server_is_up()
    return test_account(PRINCIPAL_NAME, documents={"api-status.md": STATUS_FACT})


def test_status_question_auto_replies() -> None:
    """A GREEN topic with a grounded answer must AUTO_REPLY."""
    client = _client()
    try:
        _assert_scenario(client, SCENARIOS[0])
    finally:
        client.destroy()


def test_pricing_request_escalates() -> None:
    """A RED topic must ESCALATE regardless of phrasing."""
    client = _client()
    try:
        _assert_scenario(client, SCENARIOS[1])
    finally:
        client.destroy()


def test_social_chatter_stays_silent() -> None:
    """Small talk aimed at another participant must stay SILENT."""
    client = _client()
    try:
        _assert_scenario(client, SCENARIOS[2])
    finally:
        client.destroy()


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

def main(argv: list[str]) -> int:
    """Run every scenario against the live server and print a report."""
    verbose = "-v" in argv or "--verbose" in argv
    ensure_server_is_up()
    print(f"DELEG8 Sprint 1 verification against {BASE_URL}\n")
    client = test_account(PRINCIPAL_NAME, documents={"api-status.md": STATUS_FACT})
    print(f"       account  : {client.email} (principal {client.principal})\n")

    failures = 0
    for scenario in SCENARIOS:
        try:
            decision = _assert_scenario(client, scenario)
            verdict, detail = "PASS", decision["reason"]
        except AssertionError as exc:
            failures += 1
            verdict, detail, decision = "FAIL", str(exc), {}
        except ApiError as exc:
            failures += 1
            verdict, detail, decision = "FAIL", str(exc), {}

        print(f"[{verdict}] {scenario.name}")
        print(f"       prompt   : {scenario.transcript!r}")
        print(f"       expected : {scenario.expected_state}")
        if decision:
            print(f"       got      : {decision['state']}  (topic={decision.get('topic')}, tier={decision.get('tier')})")
        print(f"       detail   : {detail}")
        if verbose and decision:
            print("       trace    :")
            for step in decision.get("trace", []):
                print(f"         - {step['stage']}: {step['outcome']}")
            if decision.get("suggested_response"):
                print(f"       response : {decision['suggested_response']}")
        print()

    total = len(SCENARIOS)
    print(f"{total - failures}/{total} scenarios passed.")
    if not failures:
        client.destroy()
    else:
        print(f"Leaving {client.email} in place so the failure can be investigated.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
