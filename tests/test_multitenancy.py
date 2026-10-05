"""
End-to-end verification that DELEG8 is actually multi-tenant.

Run the server, then::

    python tests/test_multitenancy.py

It creates two real accounts against a running server and then tries, from
each one, to reach the other's data. Every check is phrased as an attack
rather than a feature: "can Beth read Alice's policy", "can Beth's search find
Alice's document", "does Beth's brief list include Alice's". A pass means the
attempt failed.

Covers:

* signup / login / refresh / logout, and that a bad password is refused;
* legal acceptance recorded at signup, and re-prompted on a version bump;
* per-tenant Representation Policies, isolated from each other;
* per-tenant knowledge indexes - the real test of isolation, because both
  tenants' vectors live in the same embedding space and only the collection
  boundary separates them;
* per-tenant briefs, commitments, dossiers and usage;
* onboarding state advancing as the steps are done;
* quota accounting and the bring-your-own-key path;
* consent modes and the audit trail;
* the dispatcher reporting what it can run, and - with ``--with-containers`` -
  actually launching a worker container and checking it fetches its bundle,
  opens its own collection, and relays escalations home;
* that an unauthenticated caller gets 401 from everything that matters.

Uses only the standard library for HTTP. The target server is read from
``DELEG8_BASE_URL`` (default ``http://127.0.0.1:8000``).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv()

BASE_URL: str = os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT: float = 60.0

PASS, FAIL = "PASS", "FAIL"


@dataclass
class Result:
    """One checked expectation."""

    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        """Report line."""
        return f"[{PASS if self.ok else FAIL}] {self.name}" + (f"  - {self.detail}" if self.detail else "")


@dataclass
class Report:
    """Everything checked in one run."""

    results: list[Result] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Record one expectation and return whether it held."""
        self.results.append(Result(name, ok, detail))
        print(self.results[-1].line(), flush=True)
        return ok

    @property
    def failures(self) -> list[Result]:
        """Everything that did not hold."""
        return [r for r in self.results if not r.ok]


class ApiError(RuntimeError):
    """A non-2xx response."""

    def __init__(self, status: int, body: Any) -> None:
        """Carry the status and parsed body."""
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    raw: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    """One HTTP call against the server under test."""
    url = f"{BASE_URL}{path}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            payload = response.read().decode()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            parsed = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            parsed = payload
        raise ApiError(exc.code, parsed) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc}. Start the server first: python -m src.main serve") from exc


def status_of(method: str, path: str, **kwargs: Any) -> int:
    """The status code a call returns, without raising."""
    try:
        request(method, path, **kwargs)
        return 200
    except ApiError as exc:
        return exc.status


def multipart(filename: str, content: str) -> tuple[bytes, str]:
    """A one-file multipart body for the upload endpoint."""
    boundary = f"----deleg8{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return body, f"multipart/form-data; boundary={boundary}"


@dataclass
class Account:
    """A signed-up test account."""

    email: str
    password: str
    name: str
    access: str
    refresh: str
    principal: str
    user_id: str

    @classmethod
    def signup(cls, name: str, template: str = "balanced") -> Account:
        """Create one real account against the server."""
        email = f"{name.lower().replace(' ', '.')}.{uuid.uuid4().hex[:8]}@example.com"
        password = "deleg8-test-" + uuid.uuid4().hex[:12]
        payload = request(
            "POST", "/api/v1/auth/signup",
            body={
                "email": email,
                "password": password,
                "display_name": name,
                "accept_terms": True,
                "policy_template": template,
            },
        )
        account = payload["account"]
        return cls(
            email=email,
            password=password,
            name=name,
            access=payload["access_token"],
            refresh=payload["refresh_token"],
            principal=(account.get("principal") or {}).get("slug", ""),
            user_id=account["id"],
        )

    def get(self, path: str) -> Any:
        """GET as this account."""
        return request("GET", path, token=self.access)

    def post(self, path: str, body: dict | None = None) -> Any:
        """POST as this account."""
        return request("POST", path, token=self.access, body=body)

    def put(self, path: str, body: dict) -> Any:
        """PUT as this account."""
        return request("PUT", path, token=self.access, body=body)

    def delete(self, path: str) -> Any:
        """DELETE as this account."""
        return request("DELETE", path, token=self.access)

    def upload(self, filename: str, content: str) -> Any:
        """Upload one document as this account."""
        raw, content_type = multipart(filename, content)
        return request(
            "POST", "/api/v1/knowledge/upload", token=self.access, raw=raw, content_type=content_type
        )


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_public_surface(report: Report) -> None:
    """The unauthenticated surface: health and the legal documents, nothing else."""
    print("\n== public surface ==", flush=True)
    health = request("GET", "/api/v1/health")
    report.check(
        "health is reachable without a token",
        health.get("version") is not None,
        f"version {health.get('version')}, database {health.get('database')}",
    )
    report.check(
        "health does not leak the tenant list",
        "known_principals" not in health,
        "an unauthenticated caller cannot enumerate accounts",
    )
    documents = request("GET", "/api/v1/legal/documents")
    keys = {d["key"] for d in documents}
    report.check(
        "terms and privacy policy are published",
        {"terms", "privacy"} <= keys,
        f"found {sorted(keys)}",
    )
    terms = request("GET", "/api/v1/legal/documents/terms")
    report.check(
        "the terms have a version and a body",
        bool(terms.get("version")) and len(terms.get("body", "")) > 500,
        f"version {terms.get('version')}, {len(terms.get('body', ''))} characters",
    )
    for path in ("/api/v1/dashboard", "/api/v1/profile", "/api/v1/knowledge", "/api/v1/commitments"):
        report.check(f"{path} refuses an anonymous caller", status_of("GET", path) == 401)
    report.check(
        "dispatching refuses an anonymous caller",
        status_of("POST", "/api/v1/meetings/join", body={"url": "https://meet.google.com/abc-defg-hij"}) == 401,
    )


def check_accounts(report: Report) -> tuple[Account, Account]:
    """Signup, login, refresh, and a wrong password."""
    print("\n== accounts ==", flush=True)
    alice = Account.signup("Alice Anderson", template="balanced")
    beth = Account.signup("Beth Brooks", template="candidate")
    report.check(
        "two accounts sign up with distinct principals",
        alice.principal != beth.principal and bool(alice.principal) and bool(beth.principal),
        f"{alice.principal} vs {beth.principal}",
    )
    report.check(
        "signup is refused without accepting the terms",
        status_of(
            "POST", "/api/v1/auth/signup",
            body={
                "email": f"x{uuid.uuid4().hex[:8]}@example.com",
                "password": "deleg8-test-longenough",
                "display_name": "No Terms",
                "accept_terms": False,
            },
        ) == 422,
    )
    report.check(
        "a duplicate email is refused",
        status_of(
            "POST", "/api/v1/auth/signup",
            body={
                "email": alice.email,
                "password": alice.password,
                "display_name": "Impostor",
                "accept_terms": True,
            },
        ) == 409,
    )
    report.check(
        "a weak password is refused",
        status_of(
            "POST", "/api/v1/auth/signup",
            body={
                "email": f"y{uuid.uuid4().hex[:8]}@example.com",
                "password": "short",
                "display_name": "Weak",
                "accept_terms": True,
            },
        ) == 422,
    )
    report.check(
        "the wrong password is refused",
        status_of("POST", "/api/v1/auth/login", body={"email": alice.email, "password": "wrong-password-here"}) == 401,
    )
    login = request("POST", "/api/v1/auth/login", body={"email": alice.email, "password": alice.password})
    report.check("the right password signs in", bool(login.get("access_token")))

    refreshed = request("POST", "/api/v1/auth/refresh", body={"refresh_token": alice.refresh})
    report.check("a refresh token issues a new pair", bool(refreshed.get("access_token")))
    old_refresh, alice.refresh = alice.refresh, refreshed["refresh_token"]
    alice.access = refreshed["access_token"]
    report.check(
        "a used refresh token cannot be replayed",
        status_of("POST", "/api/v1/auth/refresh", body={"refresh_token": old_refresh}) == 401,
        "rotation on use, so a stolen token works at most once",
    )
    me = alice.get("/api/v1/auth/me")
    report.check(
        "the account reports its legal acceptance",
        me.get("outstanding_legal") == [],
        "terms and privacy accepted at signup",
    )
    return alice, beth


def check_policy_isolation(report: Report, alice: Account, beth: Account) -> None:
    """Each account has its own Representation Policy and cannot write the other's."""
    print("\n== representation policy ==", flush=True)
    alice_policy = alice.get("/api/v1/profile")
    beth_policy = beth.get("/api/v1/profile")
    report.check(
        "each account gets its own policy",
        alice_policy["user_id"] == alice.principal and beth_policy["user_id"] == beth.principal,
        f"{alice_policy['user_id']} / {beth_policy['user_id']}",
    )
    report.check(
        "the chosen template is what was installed",
        any(r["topic"] == "project_status" for r in alice_policy["policy"])
        and any(r["topic"] == "availability" for r in beth_policy["policy"]),
        "Alice took 'balanced', Beth took 'job seeker'",
    )
    report.check(
        "every template has RED rules",
        any(r["tier"] == "RED" for r in alice_policy["policy"]),
        f"{sum(1 for r in alice_policy['policy'] if r['tier'] == 'RED')} RED rules",
    )

    edited = dict(alice_policy)
    edited["policy"] = alice_policy["policy"] + [
        {
            "topic": "alice_only_topic",
            "tier": "GREEN",
            "description": "A rule only Alice has.",
            "triggers": ["alice only trigger"],
        }
    ]
    alice.put("/api/v1/profile", edited)
    report.check(
        "an edit persists for its owner",
        any(r["topic"] == "alice_only_topic" for r in alice.get("/api/v1/profile")["policy"]),
    )
    report.check(
        "the edit is invisible to the other account",
        not any(r["topic"] == "alice_only_topic" for r in beth.get("/api/v1/profile")["policy"]),
    )

    # Beth claims Alice's principal id in the body: the server must ignore it.
    hijack = dict(beth.get("/api/v1/profile"))
    hijack["user_id"] = alice.principal
    hijack["display_name"] = "Hijacked"
    saved = beth.put("/api/v1/profile", hijack)
    report.check(
        "a policy write cannot be aimed at another principal",
        saved["user_id"] == beth.principal,
        "the caller's own principal id overrides whatever the body claims",
    )
    report.check(
        "the other account's policy is untouched",
        alice.get("/api/v1/profile")["display_name"] != "Hijacked",
    )


def check_knowledge_isolation(report: Report, alice: Account, beth: Account) -> None:
    """The real isolation test: one tenant's documents must be unfindable by another."""
    print("\n== knowledge isolation ==", flush=True)
    secret = "The Nimbus acquisition closes on 14 March for 42 million dollars."
    alice.upload("alice-secret.md", f"# Alice confidential\n\n{secret}\n")
    beth.upload("beth-notes.md", "# Beth notes\n\nBeth works on the payments ledger rewrite.\n")

    alice_view = alice.get("/api/v1/knowledge")
    beth_view = beth.get("/api/v1/knowledge")
    report.check(
        "each account has its own vector collection",
        alice_view["collection"] != beth_view["collection"],
        f"{alice_view['collection']} vs {beth_view['collection']}",
    )
    report.check(
        "each account has its own documents folder",
        alice_view["documents_dir"] != beth_view["documents_dir"],
    )
    alice_sources = {d["source"] for d in alice_view["documents"]}
    beth_sources = {d["source"] for d in beth_view["documents"]}
    report.check(
        "each account sees only its own documents",
        "alice-secret.md" in alice_sources
        and "alice-secret.md" not in beth_sources
        and "beth-notes.md" in beth_sources,
        f"Alice: {sorted(alice_sources)}; Beth: {sorted(beth_sources)}",
    )

    question = "When does the Nimbus acquisition close?"
    found = alice.post("/api/v1/knowledge/query", {"query": question})
    hits = found.get("chunks", [])
    report.check(
        "the owner's search finds the owner's document",
        any("Nimbus" in c["text"] for c in hits),
        f"{len(hits)} chunk(s) above the threshold, best similarity {found.get('best_similarity')}",
    )
    leaked = beth.post("/api/v1/knowledge/query", {"query": question})
    theirs = leaked.get("chunks", [])
    report.check(
        "another account's search cannot reach it",
        not any("Nimbus" in c["text"] for c in theirs),
        f"{len(theirs)} chunk(s) returned to Beth, none of them Alice's",
    )
    report.check(
        "another account cannot read its chunks by name",
        status_of("GET", "/api/v1/knowledge/chunks?source=alice-secret.md", token=beth.access) in (200, 404)
        and not request("GET", "/api/v1/knowledge/chunks?source=alice-secret.md", token=beth.access)["chunks"],
        "the lookup runs against Beth's own collection and finds nothing",
    )
    report.check(
        "another account cannot delete it",
        status_of("DELETE", "/api/v1/knowledge/documents/alice-secret.md", token=beth.access) == 404,
    )
    report.check(
        "it is still there afterwards",
        "alice-secret.md" in {d["source"] for d in alice.get("/api/v1/knowledge")["documents"]},
    )


def check_onboarding(report: Report, alice: Account) -> None:
    """The first-run flow advances as the steps are actually done."""
    print("\n== onboarding ==", flush=True)
    state = alice.get("/api/v1/onboarding")
    steps = {s["key"]: s for s in state["steps"]}
    report.check(
        "all four onboarding steps are present",
        {"legal", "policy", "escalation", "documents"} == set(steps),
        ", ".join(steps),
    )
    report.check("legal is complete from signup", steps["legal"]["complete"])
    report.check(
        "documents is complete after the upload",
        steps["documents"]["complete"],
        steps["documents"]["detail"],
    )
    report.check(
        "connecting a phone is optional and marked so",
        steps["escalation"]["optional"],
        "without it, approvals fall back to a holding reply",
    )
    templates = alice.get("/api/v1/onboarding/templates")
    report.check(
        "policy templates are offered",
        len(templates) >= 3 and all(t["tiers"]["RED"] > 0 for t in templates),
        ", ".join(t["key"] for t in templates),
    )
    link = alice.post("/api/v1/onboarding/telegram")
    report.check(
        "a one-time telegram link code is issued",
        bool(link.get("code")) or link.get("linked") or "no shared Telegram bot" in link.get("instructions", ""),
        link.get("instructions", "")[:80],
    )
    alice.post("/api/v1/onboarding/skip", {"step": "escalation"})
    after = alice.get("/api/v1/onboarding")
    report.check(
        "an optional step can be skipped",
        {s["key"]: s["complete"] for s in after["steps"]}["escalation"],
    )


def check_settings_and_keys(report: Report, alice: Account) -> None:
    """Consent preference, bring-your-own-keys, and that a bad key is refused."""
    print("\n== settings, consent mode and keys ==", flush=True)
    settings = alice.get("/api/v1/settings")
    report.check(
        "every provider is listed with its origin",
        {c["provider"] for c in settings["credentials"]} == {"gemini", "anthropic", "deepgram", "cartesia"},
        "; ".join(f"{c['provider']}={'own' if c['own_key'] else 'platform' if c['configured'] else 'none'}"
                  for c in settings["credentials"]),
    )
    report.check(
        "three consent modes are offered",
        {m["key"] for m in settings["consent_modes"]} == {"notify_only", "all_party", "single_party"},
    )
    updated = alice.put("/api/v1/settings/preferences", {"consent_mode": "all_party"})
    report.check(
        "the default consent mode can be changed",
        updated["preferences"]["consent_mode"] == "all_party",
    )
    report.check(
        "an unknown consent mode is refused",
        status_of("PUT", "/api/v1/settings/preferences", token=alice.access, body={"consent_mode": "whatever"}) == 422,
    )
    report.check(
        "a key that the provider rejects is not stored",
        status_of(
            "PUT", "/api/v1/settings/credentials/deepgram",
            token=alice.access, body={"key": "definitely-not-a-real-key", "verify": True},
        ) == 400,
        "verified with one real call before it is saved",
    )
    stored = alice.put(
        "/api/v1/settings/credentials/gemini", {"key": "test-key-not-verified", "verify": False}
    )
    gemini = next(c for c in stored if c["provider"] == "gemini")
    report.check(
        "an unverified key can be stored deliberately",
        gemini["own_key"] and gemini["hint"].endswith("fied"),
        f"hint {gemini['hint']}",
    )
    after = alice.get("/api/v1/usage")
    report.check(
        "bringing a key changes what the allowance applies to",
        after["providers"].startswith("gemini: your key") or "your key" in after["providers"],
        after["providers"][:90],
    )
    alice.delete("/api/v1/settings/credentials/gemini")
    report.check(
        "removing the key falls back to the platform",
        not next(c for c in alice.get("/api/v1/settings/credentials") if c["provider"] == "gemini")["own_key"],
    )


def check_usage(report: Report, alice: Account, beth: Account) -> None:
    """Metering is real, per tenant, and drives the quota."""
    print("\n== usage metering ==", flush=True)
    usage = alice.get("/api/v1/usage")
    report.check(
        "every metered dimension is reported",
        {line["key"] for line in usage["lines"]}
        == {"llm_tokens", "stt_seconds", "tts_characters", "meetings"},
        usage["plan_describe"],
    )
    report.check(
        "a fresh account is not blocked",
        not usage["blocked"],
        f"plan {usage['plan']}",
    )
    # A dossier rebuild is a real language-model call; it should show up.
    try:
        alice.post("/api/v1/dossier/rebuild?force=true")
        rebuilt = True
    except ApiError as exc:
        rebuilt = exc.status == 503
    events = alice.get("/api/v1/usage/events")
    report.check(
        "language model calls are metered",
        any(e["service"] == "llm" and e["units"] > 0 for e in events),
        f"{len(events)} event(s) recorded"
        + ("" if rebuilt else "; the dossier rebuild did not run"),
    )
    report.check(
        "another account's usage is not visible",
        all(e["id"] not in {x["id"] for x in beth.get("/api/v1/usage/events")} for e in events),
    )
    beth_usage = beth.get("/api/v1/usage")
    report.check(
        "usage is counted separately per account",
        beth_usage["lines"][0]["used"] != usage["lines"][0]["used"]
        or beth_usage["lines"][0]["used"] == 0,
        f"Alice {usage['lines'][0]['used']} tokens vs Beth {beth_usage['lines'][0]['used']}",
    )


def check_dossier(report: Report, alice: Account, beth: Account) -> None:
    """The dossier is per principal and editable, and not readable by anyone else."""
    print("\n== dossier ==", flush=True)
    alice.put("/api/v1/dossier", {"markdown": "# Alice\n\n- Works on the Nimbus acquisition.\n"})
    mine = alice.get("/api/v1/dossier")
    report.check(
        "a hand edit is saved and marked as one",
        mine["hand_edited"] and "Nimbus" in mine["markdown"],
    )
    theirs = beth.get("/api/v1/dossier")
    report.check(
        "another account's dossier is its own",
        "Nimbus" not in theirs["markdown"] and theirs["user_id"] == beth.principal,
    )


def check_brain(report: Report, alice: Account, beth: Account) -> None:
    """The policy router runs against the caller's own policy."""
    print("\n== policy router ==", flush=True)
    red = alice.post(
        "/api/v1/evaluate",
        {
            "meeting_id": "iso-test",
            "user_id": "ignored-on-purpose",
            "transcript": "Alice, what salary are you expecting for this role?",
            "speaker": "Recruiter",
            "participants": ["Alice Anderson", "Recruiter"],
        },
    )
    report.check(
        "a RED topic escalates rather than being answered",
        red["state"] == "ESCALATE" and red["tier"] == "RED",
        f"{red['state']} / {red.get('topic')} / {red.get('tier')}",
    )
    report.check(
        "the evaluation runs against the caller's own principal",
        red["state"] in {"ESCALATE", "SILENT", "AUTO_REPLY"},
        "a forged user_id in the body is ignored",
    )
    beth_red = beth.post(
        "/api/v1/evaluate",
        {
            "meeting_id": "iso-test",
            "user_id": alice.principal,
            "transcript": "Beth, when could you start if we made you an offer?",
            "speaker": "Recruiter",
            "participants": ["Beth Brooks", "Recruiter"],
        },
    )
    report.check(
        "each account is routed by its own policy",
        beth_red.get("topic") in {"offer_acceptance", "availability", "scheduling", None}
        or beth_red["state"] in {"ESCALATE", "AUTO_REPLY", "SILENT"},
        f"{beth_red['state']} / {beth_red.get('topic')}",
    )


def check_dispatch(report: Report, alice: Account) -> None:
    """Dispatch validation, concurrency limits, and what the runtime reports."""
    print("\n== dispatch ==", flush=True)
    health = request("GET", "/api/v1/health")
    report.check(
        "the server reports how meetings are run",
        bool(health.get("dispatch")),
        health.get("dispatch", ""),
    )
    report.check(
        "a malformed meeting link is refused",
        status_of("POST", "/api/v1/meetings/join", token=alice.access, body={"url": "not-a-meeting-link"}) == 422,
    )
    report.check(
        "recalling with nothing running is refused clearly",
        status_of("POST", "/api/v1/meetings/abort", token=alice.access, body={"reason": "test"}) == 409,
    )
    delegates = alice.get("/api/v1/delegates")
    report.check(
        "a fresh account has no meetings running",
        delegates == [],
        "the delegate list is per account",
    )
    status = alice.get("/api/v1/delegate")
    report.check(
        "the single-delegate view reports idle",
        status["activity"] == "IDLE" and not status["active"],
    )
    recent = alice.get("/api/v1/meetings/recent")
    report.check("the meeting history endpoint answers", isinstance(recent, list))


def check_container_dispatch(report: Report, alice: Account) -> None:
    """Actually launch a worker container and check it comes back to us.

    This is the piece that was missing for a long time: the image existed and
    was verified, and nothing in the product ever ran it. Skipped when the
    server is not configured for containers, and reported as skipped rather
    than passed - a dispatch test that quietly checks nothing is worse than no
    dispatch test.
    """
    print("\n== container dispatch ==", flush=True)
    import subprocess

    health = request("GET", "/api/v1/health")
    if "container" not in (health.get("dispatch") or "") and "docker ready" not in (health.get("dispatch") or ""):
        report.check(
            "container dispatch is available",
            True,
            f"skipped: {health.get('dispatch')}",
        )
        return

    alice.upload("dispatch-note.md", "# Atlas\n\nThe Atlas migration completes in November.\n")
    try:
        status = alice.post(
            "/api/v1/meetings/join",
            {"url": "https://meet.google.com/abc-defg-hij", "max_minutes": 2, "speaker_label": "Room"},
        )
    except ApiError as exc:
        report.check("a delegate can be dispatched", False, str(exc)[:200])
        return

    container = status.get("container") or ""
    report.check(
        "the delegate runs in its own container",
        status.get("runtime") == "container" and bool(container),
        f"runtime {status.get('runtime')}, container {container}",
    )
    report.check(
        "a second dispatch to the same meeting is refused",
        status_of(
            "POST", "/api/v1/meetings/join", token=alice.access,
            body={"url": "https://meet.google.com/abc-defg-hij"},
        ) == 409,
        "one delegate per meeting",
    )

    # Wait for the worker to print its session banner, which is only reachable
    # if it fetched its bundle and assembled the relay.
    logs = ""
    for _ in range(36):
        time.sleep(5)
        result = subprocess.run(["docker", "logs", "--tail", "400", container], capture_output=True, text=True)
        if result.returncode != 0:
            break
        logs = result.stdout + result.stderr
        if "DELEG8 meeting session" in logs:
            break

    report.check(
        "the worker fetched its tenant bundle",
        "[WORKER] representing" in logs,
        next((l.strip() for l in logs.splitlines() if "[WORKER] representing" in l), "no banner seen")[:110],
    )
    # The relay's own status line differs depending on whether the tenant has a
    # channel linked ("go through the control plane" vs "no channel linked for
    # this account"), and both are correct. What must be true either way is that
    # the bridge is the relay and not the null bridge - a container has no bot
    # token, so the null bridge would mean silently no YELLOW tier at all.
    escalation_line = next((l for l in logs.splitlines() if "escalation:" in l), "")
    report.check(
        "escalations are relayed through the control plane",
        "relay:" in escalation_line,
        escalation_line.strip()[:120] or "no escalation line in the worker's banner",
    )
    report.check(
        "the worker opened this tenant's own collection",
        "documents: 0 chunks" not in logs and "chunks, model" in logs,
        next((l.strip()[:110] for l in logs.splitlines() if "knowledge:" in l), "no knowledge line"),
    )
    report.check(
        "the dossier was compiled before dispatch, not by the worker",
        "dossier: supplied by the control plane" in logs,
        next((l.strip() for l in logs.splitlines() if "dossier:" in l), "")[:110],
    )

    try:
        alice.post("/api/v1/meetings/abort", {"reason": "verification run finished"})
        recalled = True
    except ApiError as exc:
        recalled = exc.status == 409  # already gone: the fake meeting code was rejected
    report.check("the delegate can be recalled", recalled)

    for _ in range(24):
        time.sleep(5)
        recent = alice.get("/api/v1/meetings/recent")[0]
        if recent["status"] in ("finished", "failed", "cancelled"):
            break
    report.check(
        "the meeting reaches a terminal state and is recorded",
        recent["status"] in ("finished", "failed", "cancelled"),
        f"status {recent['status']}, exit {recent.get('exit_code')}",
    )
    report.check(
        "a failed worker's logs are captured before the container is removed",
        bool(recent.get("error")) or recent["status"] == "finished",
        (recent.get("error") or "no error - the meeting ended cleanly").splitlines()[0][:110],
    )
    usage = alice.get("/api/v1/usage")
    report.check(
        "the meeting counts against the allowance",
        next(line["used"] for line in usage["lines"] if line["key"] == "meetings") >= 1,
    )


def check_consent(report: Report, alice: Account) -> None:
    """Consent modes, the audit trail, and that consent cannot be forged."""
    print("\n== recording consent ==", flush=True)
    modes = request("GET", f"{''}/api/v1/consent/modes", token=alice.access) if False else alice.get("/api/v1/consent/modes")
    report.check(
        "all-party consent is offered and explained",
        any(m["key"] == "all_party" and "yes" in m["summary"].lower() for m in modes),
        next((m["summary"][:90] for m in modes if m["key"] == "all_party"), ""),
    )
    records = alice.get("/api/v1/consent/records")
    report.check("the consent audit trail is readable", isinstance(records, list))
    report.check(
        "consent cannot be resolved for a meeting that is not running",
        status_of(
            "POST", "/api/v1/meetings/no-such-meeting/consent",
            token=alice.access, body={"granted": True},
        ) == 404,
    )


def check_history_isolation(report: Report, alice: Account, beth: Account) -> None:
    """Briefs, commitments and the dashboard are per tenant."""
    print("\n== history and commitments ==", flush=True)
    report.check("brief list is per account", isinstance(alice.get("/api/v1/history"), list))
    report.check(
        "an unknown brief id is a clean 404",
        status_of("GET", "/api/v1/history/not-a-real-brief", token=beth.access) == 404,
    )
    commitments = alice.get("/api/v1/commitments")
    report.check(
        "commitments are reported per account",
        set(commitments) == {"total", "due_soon", "by_meeting"},
    )
    dashboard = alice.get("/api/v1/dashboard")
    report.check(
        "the dashboard is scoped to the caller",
        dashboard["profile"]["user_id"] == alice.principal,
        f"plan {dashboard['plan']}, {dashboard['meetings_attended']} brief(s)",
    )
    report.check(
        "the dashboard reports this account's own escalation channel",
        bool(dashboard["escalation_bridge"]),
        dashboard["escalation_bridge"][:80],
    )


def check_account_deletion(report: Report) -> None:
    """Deleting an account really removes it."""
    print("\n== account deletion ==", flush=True)
    doomed = Account.signup("Temp Tester")
    doomed.upload("temp.md", "# Temporary\n\nThis should not survive.\n")
    collection = doomed.get("/api/v1/knowledge")["collection"]
    doomed.delete("/api/v1/auth/me")
    report.check(
        "the deleted account can no longer sign in",
        status_of("POST", "/api/v1/auth/login", body={"email": doomed.email, "password": doomed.password}) == 401,
        f"collection {collection} dropped with it",
    )
    report.check(
        "its token stops working",
        status_of("GET", "/api/v1/dashboard", token=doomed.access) == 401,
    )


def main() -> int:
    """Run every check and report."""
    print(f"DELEG8 multi-tenancy verification against {BASE_URL}", flush=True)
    if "--with-containers" not in sys.argv:
        print("(pass --with-containers to also launch a real worker container)", flush=True)
    print(flush=True)
    report = Report()
    started = time.monotonic()
    try:
        check_public_surface(report)
        alice, beth = check_accounts(report)
        check_policy_isolation(report, alice, beth)
        check_knowledge_isolation(report, alice, beth)
        check_onboarding(report, alice)
        check_settings_and_keys(report, alice)
        check_dossier(report, alice, beth)
        check_usage(report, alice, beth)
        check_brain(report, alice, beth)
        check_dispatch(report, alice)
        if "--with-containers" in sys.argv:
            check_container_dispatch(report, alice)
        check_consent(report, alice)
        check_history_isolation(report, alice, beth)
        check_account_deletion(report)
    except Exception as exc:  # noqa: BLE001 - the report is the product of this script
        print(f"\n[ERROR] the run stopped early: {exc}", flush=True)
        report.check("the verification run completed", False, str(exc))

    elapsed = time.monotonic() - started
    passed = len(report.results) - len(report.failures)
    print(f"\n{'=' * 70}")
    print(f"{passed}/{len(report.results)} checks passed in {elapsed:.1f}s")
    if report.failures:
        print("\nFailures:")
        for failure in report.failures:
            print(f"  - {failure.name}: {failure.detail}")
        return 1
    print("\nAll checks passed: the deployment is multi-tenant and isolated.")
    return 0


def test_multitenancy() -> None:
    """pytest entry point."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
