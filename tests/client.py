"""
A throwaway account and a signed-in HTTP client, for the tests that need a server.

Every ``/api/v1`` endpoint worth testing is now behind authentication and
scoped to one tenant, so a test that wants to evaluate a transcript has to be
somebody. :func:`test_account` creates a real account, gives it a policy and a
document to work from, and hands back a client that carries its token.

Accounts are real rows and are left behind deliberately: a test that fails is
much easier to investigate when its tenant still exists. Call
:meth:`TestClient.destroy` to clean one up when the run succeeded.

Standard library only, like the rest of the test scripts here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

load_dotenv()

BASE_URL: str = os.getenv("DELEG8_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT_SECONDS: float = 60.0


class ApiError(RuntimeError):
    """A non-2xx response from the API."""

    def __init__(self, status: int, body: Any) -> None:
        """Carry the status code and the parsed body."""
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


@dataclass
class TestClient:
    """A signed-in client for one throwaway account."""

    email: str
    password: str
    display_name: str
    access: str
    refresh: str
    principal: str
    user_id: str

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        raw: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        """One authenticated request."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Authorization", f"Bearer {self.access}")
        if data is not None:
            request.add_header("Content-Type", content_type or "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read().decode()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            text = exc.read().decode()
            try:
                parsed = json.loads(text) if text else None
            except json.JSONDecodeError:
                parsed = text
            raise ApiError(exc.code, parsed) from exc

    def get(self, path: str) -> Any:
        """GET as this account."""
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        """POST as this account."""
        return self.call("POST", path, body)

    def put(self, path: str, body: dict) -> Any:
        """PUT as this account."""
        return self.call("PUT", path, body)

    def upload(self, filename: str, content: str) -> Any:
        """Upload one Markdown document into this account's own index."""
        boundary = f"----deleg8{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        return self.call(
            "POST", "/api/v1/knowledge/upload", raw=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def destroy(self) -> None:
        """Delete the account and everything it owns."""
        try:
            self.call("DELETE", "/api/v1/auth/me")
        except ApiError:
            pass


def server_is_up() -> dict[str, Any]:
    """The health payload, or exit with a clear message when the server is down."""
    import sys

    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/v1/health", timeout=10.0) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(
            f"Cannot reach DELEG8 at {BASE_URL} ({exc}).\n"
            "Start the server first:  python -m src.main serve"
        )


def test_account(
    name: str = "Test Principal",
    *,
    template: str = "balanced",
    documents: dict[str, str] | None = None,
) -> TestClient:
    """Create a throwaway account, optionally seeded with documents.

    Raises:
        ApiError: signup was refused.
    """
    email = f"test.{uuid.uuid4().hex[:10]}@example.com"
    password = "deleg8-test-" + uuid.uuid4().hex[:12]
    request = urllib.request.Request(
        f"{BASE_URL}/api/v1/auth/signup",
        data=json.dumps(
            {
                "email": email,
                "password": password,
                "display_name": name,
                "accept_terms": True,
                "policy_template": template,
            }
        ).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode()) from exc

    account = payload["account"]
    client = TestClient(
        email=email,
        password=password,
        display_name=name,
        access=payload["access_token"],
        refresh=payload["refresh_token"],
        principal=(account.get("principal") or {}).get("slug", ""),
        user_id=account["id"],
    )
    for filename, content in (documents or {}).items():
        client.upload(filename, content)
    return client
