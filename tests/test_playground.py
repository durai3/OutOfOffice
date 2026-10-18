"""
End-to-end check of the browser playground against a running server and the real Gemini brain.

Run the server, then::

    python tests/test_playground.py            # asserts, prints a transcript of the meeting
    python tests/test_playground.py --keep     # leave the throwaway account behind for inspection

It signs up a throwaway account, loads the sample knowledge base, starts a
simulated meeting, and drives the three behaviours the product is about:

* it **answers from documents** and the answer is grounded (nothing invented);
* it **will not improvise on RED / YELLOW topics** - money and dates go to a
  human, who approves or declines from the "phone", and the delegate then says
  what they chose;
* it **records what it is asked to do**, and the meeting ends with a briefing
  and an action item.

Model output varies run to run, so the assertions target what is *guaranteed*
by the architecture (the policy engine and the output guard are deterministic)
and keep wording checks to facts that appear verbatim in the sample documents.

Exit codes: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from client import ApiError, server_is_up, test_account  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        print(f"  {GREEN}PASS{RESET} {name}")
    else:
        print(f"  {RED}FAIL{RESET} {name}{(' - ' + detail) if detail else ''}")
        _failures.append(name)


def say(client, speaker: str, text: str) -> dict:
    """One utterance; print what the delegate did."""
    started = time.monotonic()
    out = client.post("/api/v1/playground/sessions/current/turns", {"speaker": speaker, "text": text})
    result = out["result"]
    spoken = result.get("response")
    print(f"  {DIM}{speaker}:{RESET} {text}")
    print(
        f"    {BOLD}{result['state']}{RESET} / {result.get('turn_type')} "
        f"({time.monotonic() - started:.1f}s) -> {spoken!r}"
    )
    return out


def main() -> int:
    """Run the scenario."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    health = server_is_up()
    print(f"server: {health['llm']}")
    client = test_account("Alex Morgan")

    try:
        print(f"\n{BOLD}1. sample data{RESET}")
        loaded = client.post("/api/v1/playground/sample-data")
        check("three sample documents were indexed", len(loaded["loaded"]) == 3, str(loaded))
        check("a dossier was compiled", "empty" not in loaded["dossier"].lower(), loaded["dossier"])
        again = client.post("/api/v1/playground/sample-data")
        check("loading twice is harmless (idempotent)", not again["loaded"] and len(again["skipped"]) == 3)

        scenarios = client.get("/api/v1/playground/scenarios")
        check("scenarios are served", len(scenarios) >= 3 and all(s["turns"] for s in scenarios))

        print(f"\n{BOLD}2. a meeting{RESET}")
        check("no session before one is started (null, not an error)", client.get("/api/v1/playground/sessions/current") is None)
        session = client.post("/api/v1/playground/sessions", {"participants": ["Sofia", "Jonas"]})
        check("session starts", session["meeting_id"].startswith("playground-"))
        check("Gemini is the brain", session["model"].startswith("gemini"), session["model"])

        status_turn = say(client, "Jonas", "Where do we stand on Project Atlas?")
        reply = (status_turn["result"].get("response") or "").lower()
        check("answers the status question aloud", status_turn["result"]["state"] == "AUTO_REPLY", reply)
        check("grounded in the document (68 percent)", "68" in reply, reply)

        facts_turn = say(client, "Jonas", "What database does Atlas use?")
        facts_reply = (facts_turn["result"].get("response") or "").lower()
        check("quotes the real database", "postgres" in facts_reply, facts_reply)
        grounding = facts_turn["result"]["decision"].get("grounding")
        check("the decision carries its grounding", grounding is not None)

        silent_turn = say(client, "Sofia", "Jonas, can you take the next slide please?")
        check("stays silent when the question is for someone else", not silent_turn["result"].get("response"))

        print(f"\n{BOLD}3. money goes to a human{RESET}")
        red_turn = say(client, "Sofia", "Can you give us a twenty percent discount on the renewal?")
        check("a RED topic is never answered autonomously", red_turn["result"]["state"] == "ESCALATE")
        check(
            "the delegate did not agree to a discount",
            "yes" not in (red_turn["result"].get("response") or "").lower().split()[:2],
        )

        pending = []
        for _ in range(40):
            current = client.get("/api/v1/playground/sessions/current")
            pending = [a for a in current["approvals"] if a["pending"]]
            if pending:
                break
            time.sleep(0.25)
        check("an approval request reached the 'phone'", len(pending) == 1)
        if pending:
            approval = pending[0]
            check("it names the topic", approval["topic"] == "pricing_negotiation", str(approval["topic"]))
            check("it shows what each button would say", approval["choices"] is not None)
            resolved = client.post(
                f"/api/v1/playground/sessions/current/escalations/{approval['request_id']}",
                {"outcome": "DECLINED_POLITELY"},
            )
            follow = [e for e in resolved["events"] if e["kind"] == "delegate" and e.get("followup")]
            check("after the human answers, the delegate speaks their decision", len(follow) == 1, str(resolved["events"][-3:]))
            check("the approval is closed", not [a for a in resolved["approvals"] if a["pending"]])
            try:
                client.post(
                    f"/api/v1/playground/sessions/current/escalations/{approval['request_id']}",
                    {"outcome": "APPROVED"},
                )
                check("an answered approval cannot be answered twice", False)
            except ApiError as exc:
                check("an answered approval cannot be answered twice (404)", exc.status == 404)

        print(f"\n{BOLD}4. it does not invent{RESET}")
        trap = say(client, "Sam", "What was the total budget for Atlas in 2019?")
        trap_reply = (trap["result"].get("response") or "").lower()
        digits = [tok for tok in trap_reply.replace(",", "").split() if tok.strip("$.").isdigit() and tok.strip("$.") != "2019"]
        check("no made-up budget figure", not digits, trap_reply)

        print(f"\n{BOLD}5. action items and the briefing{RESET}")
        action = say(client, "Sofia", "Please send me the updated risk register by Wednesday five pm.")
        check("an action item was recorded", len(action["result"]["commitment_ids"]) >= 1, str(action["result"]["commitment_ids"]))
        state = client.get("/api/v1/playground/sessions/current")
        check("it appears in the session's ledger", len(state["commitments"]) >= 1)

        done = client.post("/api/v1/playground/sessions/current/conclude")
        check("the meeting concludes", done["concluded"] and done["report"] is not None)
        report = done["report"]
        check("the briefing was 'sent to the phone'", len(done["outbox"]) >= 1)
        check("the briefing has an executive summary", bool(report["briefing"]["executive_summary"].strip()))
        check("the briefing lists the action item", len(report["briefing"]["action_items"]) >= 1)
        check("the ledger survives conclude for display", len(done["commitments"]) >= 1)

        tracker = client.get("/api/v1/commitments")
        check("the action item is on the Commitments page", tracker["total"] >= 1, str(tracker)[:200])
        history = client.get("/api/v1/history")
        check("the briefing is archived under Briefs", len(history) >= 1)

        try:
            client.post("/api/v1/playground/sessions/current/turns", {"speaker": "Sam", "text": "hello?"})
            check("a finished meeting refuses new turns", False)
        except ApiError as exc:
            check("a finished meeting refuses new turns (409)", exc.status == 409)

        print(f"\n{BOLD}6. isolation{RESET}")
        other = test_account("Someone Else")
        check("another account cannot see this meeting", other.get("/api/v1/playground/sessions/current") is None)
        try:
            other.post("/api/v1/playground/sessions/current/turns", {"speaker": "Eve", "text": "hello?"})
            check("another account cannot speak into this meeting", False)
        except ApiError as exc:
            check("another account cannot speak into this meeting (404)", exc.status == 404)
        other.destroy()
    finally:
        if not args.keep:
            client.destroy()

    print()
    if _failures:
        print(f"{RED}{len(_failures)} check(s) failed:{RESET} " + "; ".join(_failures))
        return 1
    print(f"{GREEN}every check passed{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
