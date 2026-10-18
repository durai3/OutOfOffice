"""
Standalone verification for the one-click judge/demo account.

Same convention as ``tests/test_organizations.py``: a plain script against the
locally configured database, cleaning up after itself.

    1. A demo account is complete at once - onboarding done, terms recorded, on
       the ``demo`` plan, flagged ``demo``.
    2. It is never an admin, even as the first account on an empty database.
    3. Two demo accounts never collide.
    4. The per-address hourly cap refuses the (limit + 1)th request.

Usage::

    python tests/test_demo_account.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.auth import routes as auth_routes
from src.auth.service import ONBOARDING_STEPS, provision_demo_account
from src.db import repo
from src.db.base import init_engine, session_scope
from src.usage.meter import plan_for

PASSED = 0
FAILED = 0


def check(label: str, condition: bool) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok  {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label}")


def main() -> int:
    init_engine()
    created = []
    try:
        with session_scope() as session:
            first = provision_demo_account(session)
            second = provision_demo_account(session)
            created = [first.id, second.id]
            check("onboarding is complete", all((first.onboarding or {}).get(k) for k in ONBOARDING_STEPS))
            check("flagged as a demo account", (first.settings or {}).get("demo") is True)
            check("on the demo plan", plan_for(first.settings).name == "Judge demo")
            check("never an admin", first.role != "admin" and second.role != "admin")
            check("two demo accounts have different emails", first.email != second.email)
            check("email cannot receive real mail", first.email.endswith(".invalid"))

        os.environ["DELEG8_DEMO_PER_IP_HOUR"] = "3"
        auth_routes._DEMO_HITS.clear()
        results = [auth_routes._demo_allowed("203.0.113.9") for _ in range(4)]
        check("hourly cap refuses the 4th request of 3", results == [True, True, True, False])
        check("a different address is unaffected", auth_routes._demo_allowed("203.0.113.10"))
    finally:
        with session_scope() as session:
            for user_id in created:
                repo.delete_user(session, user_id)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
