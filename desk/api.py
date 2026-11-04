"""HTTP client for the ResolveDesk backend, including a Server-Sent Events reader."""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import httpx

# 127.0.0.1 rather than localhost: on Windows "localhost" tries IPv6 first and adds ~2 s per request.
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# One pooled client for the whole dashboard process: keeps connections alive between calls.
_HTTP = httpx.Client(timeout=30)


class BackendError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Backend:
    def __init__(self, token: str | None = None, base_url: str = BACKEND_URL) -> None:
        self.base_url = base_url
        self.token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _request(self, method: str, path: str, timeout: float = 30, **kwargs) -> httpx.Response:
        try:
            response = _HTTP.request(method, f"{self.base_url}{path}", headers=self._headers, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise BackendError(f"Cannot reach the backend at {self.base_url} ({type(exc).__name__}).") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            if isinstance(detail, list):  # validation errors
                detail = "; ".join(d.get("msg", str(d)) for d in detail)
            raise BackendError(str(detail), response.status_code)
        return response

    def _json(self, method: str, path: str, **kwargs) -> Any:
        return self._request(method, path, **kwargs).json()

    # -- auth / health -------------------------------------------------------
    def login(self, username: str, password: str) -> dict:
        return self._json("POST", "/api/v1/auth/login", json={"username": username, "password": password})

    def me(self) -> dict:
        return self._json("GET", "/api/v1/auth/me", timeout=5)

    def health(self) -> dict:
        return self._json("GET", "/api/v1/health", timeout=5)

    # -- tickets ---------------------------------------------------------------
    def create_ticket(self, **payload) -> dict:
        return self._json("POST", "/api/v1/tickets", json=payload)

    def tickets(self, **filters) -> list[dict]:
        return self._json("GET", "/api/v1/tickets", params={k: v for k, v in filters.items() if v})

    def ticket(self, ticket_id: str) -> dict:
        return self._json("GET", f"/api/v1/tickets/{ticket_id}")

    def timeline(self, ticket_id: str) -> list[dict]:
        return self._json("GET", f"/api/v1/tickets/{ticket_id}/timeline")

    def follow(self, ticket_id: str) -> Iterator[dict]:
        """Yield events for a ticket as they happen (SSE), ending when the ticket finishes."""
        url = f"{self.base_url}/api/v1/tickets/{ticket_id}/events"
        try:
            with _HTTP.stream("GET", url, headers=self._headers, timeout=httpx.Timeout(10, read=180)) as response:
                if response.status_code >= 400:
                    raise BackendError(f"Event stream failed ({response.status_code})", response.status_code)
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "" and data_lines:
                        yield json.loads("\n".join(data_lines))
                        data_lines = []
        except httpx.HTTPError as exc:
            raise BackendError(f"Lost connection to the event stream ({type(exc).__name__}).") from exc

    def export(self, fmt: str) -> bytes:
        return self._request("GET", "/api/v1/tickets/export", params={"fmt": fmt}).content

    def log_file(self) -> bytes:
        return self._request("GET", "/api/v1/logs/tickets.csv").content

    # -- review ----------------------------------------------------------------
    def revise(self, ticket_id: str, guidance: str) -> dict:
        return self._json("POST", f"/api/v1/tickets/{ticket_id}/review/revise", json={"guidance": guidance}, timeout=120)

    def close(self, ticket_id: str, reply: str = "", note: str = "") -> dict:
        """Close a ticket; with a reply it is emailed to the customer first."""
        return self._json("POST", f"/api/v1/tickets/{ticket_id}/close", json={"reply": reply, "note": note})

    def reopen(self, ticket_id: str, note: str = "") -> dict:
        return self._json("POST", f"/api/v1/tickets/{ticket_id}/reopen", json={"note": note})

    # -- analytics / email / kb -------------------------------------------------
    def analytics(self) -> dict:
        return self._json("GET", "/api/v1/analytics")

    def outbox(self) -> dict:
        return self._json("GET", "/api/v1/email/outbox")

    def inbound_email(self, from_header: str, subject: str, body: str) -> dict:
        return self._json("POST", "/api/v1/email/inbound", json={"from_header": from_header, "subject": subject, "body": body})

    def kb_documents(self) -> list[dict]:
        return self._json("GET", "/api/v1/kb/documents")

    def kb_search(self, query: str, category: str | None, k: int) -> list[dict]:
        return self._json("POST", "/api/v1/kb/search", json={"query": query, "category": category, "k": k})
