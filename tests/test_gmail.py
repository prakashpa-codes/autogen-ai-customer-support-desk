"""Gmail MCP channel, offline: tool registration, body parsing and the send path (no Google calls)."""

import base64
import importlib

import pytest

from backend.email_service import EmailService
from core.store import TicketStore


async def _tool_names(read_only: bool, monkeypatch) -> set[str]:
    monkeypatch.setenv("GMAIL_MCP_READONLY", "1" if read_only else "0")
    import mcp_servers.gmail_server as server

    server = importlib.reload(server)  # tools are registered at import time
    return {tool.name for tool in await server.mcp.list_tools()}


async def test_read_only_server_offers_no_write_tools(monkeypatch):
    assert await _tool_names(True, monkeypatch) == {"list_unread_emails", "get_email"}
    assert await _tool_names(False, monkeypatch) == {"list_unread_emails", "get_email", "mark_as_read", "send_email"}


def test_plain_text_body_is_found_in_multipart_email():
    from mcp_servers.gmail_server import _plain_text

    encoded = base64.urlsafe_b64encode("My bat cracked.".encode()).decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PGI-PC9iPg=="}},
            {"mimeType": "text/plain", "body": {"data": encoded}},
        ],
    }
    assert _plain_text(payload) == "My bat cracked."


@pytest.fixture()
def store(tmp_path):
    s = TicketStore(tmp_path / "t.db", tmp_path / "logs")
    yield s
    s.close()


async def test_replies_go_through_gmail_sender_when_connected(store):
    service = EmailService(store)
    assert await service.send("TKT-0001", "a@example.com", "Refund", "Hello") == "stored"

    sent = []

    async def sender(ticket_id, to, subject, body):
        sent.append((ticket_id, to, subject))

    service.sender = sender
    assert await service.send("TKT-0002", "b@example.com", "Refund", "Hello") == "sent"
    assert sent == [("TKT-0002", "b@example.com", "Re: [TKT-0002] Refund")]

    async def broken(*_):
        raise RuntimeError("Gmail sign-in expired")

    service.sender = broken
    assert await service.send("TKT-0003", "c@example.com", "Refund", "Hello") == "failed"
    assert [m["delivery"] for m in store.outbox()] == ["failed", "sent", "stored"]
