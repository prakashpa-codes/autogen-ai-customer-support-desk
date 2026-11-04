"""Gmail MCP server: exposes the support mailbox as MCP tools.

Any MCP client can use it (AutoGen's McpWorkbench, Claude Desktop, VS Code). The client
starts this file as a subprocess and talks JSON-RPC over stdin/stdout, so nothing here
may print to stdout; logs go to stderr.

Least privilege: with GMAIL_MCP_READONLY=1 only the read tools are registered. The Inbox
Agent gets that read-only server; sending is done by code through a separate full server.

One-time sign-in: run `python scripts/gmail_auth.py` to create secrets/gmail_token.json.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = Path(os.getenv("GMAIL_SECRETS_DIR", str(ROOT / "secrets")))
CLIENT_FILE = SECRETS_DIR / "gmail_credentials.json"  # OAuth client downloaded from Google Cloud
TOKEN_FILE = SECRETS_DIR / "gmail_token.json"  # created by scripts/gmail_auth.py
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read, mark as read, send; never delete
INBOX_QUERY = "is:unread in:inbox -from:me"
MAX_BODY_CHARS = 8000

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="gmail-mcp %(levelname)s %(message)s")
log = logging.getLogger("gmail-mcp")


def load_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        raise RuntimeError("Gmail is not authorised yet. Run: python scripts/gmail_auth.py")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:  # testing-mode apps expire the sign-in after 7 days
                raise RuntimeError("Gmail sign-in expired. Run: python scripts/gmail_auth.py") from exc
            try:
                TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            except OSError:  # read-only mount: the refreshed token still works for this process
                log.warning("Could not save the refreshed token to %s", TOKEN_FILE)
        else:
            raise RuntimeError("Gmail token is invalid. Run: python scripts/gmail_auth.py")
    return creds


_service = None


def gmail():
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    return _service


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _plain_text(payload: dict) -> str:
    """First text/plain part of a message (walks multipart bodies)."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        if not part.get("filename"):
            text = _plain_text(part)
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------- tools

mcp = FastMCP("gmail")


def list_unread_emails(max_results: int = 10) -> list[dict]:
    """List unread emails in the support inbox (newest first): id, thread_id, from, subject, snippet."""
    found = gmail().users().messages().list(userId="me", q=INBOX_QUERY, maxResults=min(max_results, 25)).execute()
    emails = []
    for ref in found.get("messages", []):
        msg = gmail().users().messages().get(
            userId="me", id=ref["id"], format="metadata", metadataHeaders=["From", "Subject"]
        ).execute()
        headers = msg["payload"]["headers"]
        emails.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "from": _header(headers, "From"),
            "subject": _header(headers, "Subject"),
            "snippet": msg.get("snippet", ""),
        })
    return emails


def get_email(message_id: str) -> dict:
    """Read one email in full: sender, subject, date, Message-ID header and plain-text body."""
    msg = gmail().users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg["payload"]["headers"]
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": _header(headers, "From"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "message_id_header": _header(headers, "Message-ID"),
        "auto_submitted": _header(headers, "Auto-Submitted"),
        "body": _plain_text(msg["payload"])[:MAX_BODY_CHARS],
    }


def mark_as_read(message_id: str) -> str:
    """Mark an email as read so it is not picked up again."""
    gmail().users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()
    return "ok"


def send_email(to: str, subject: str, body: str, thread_id: str = "", in_reply_to: str = "") -> str:
    """Send a plain-text email. With thread_id and in_reply_to it is sent as a reply in that thread."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if in_reply_to:  # these two headers make mail clients show it as a reply in the same conversation
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)
    payload: dict = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    sent = gmail().users().messages().send(userId="me", body=payload).execute()
    log.info("sent message %s", sent["id"])
    return sent["id"]


READ_TOOLS = [list_unread_emails, get_email]
WRITE_TOOLS = [mark_as_read, send_email]

for tool in READ_TOOLS + ([] if os.getenv("GMAIL_MCP_READONLY") == "1" else WRITE_TOOLS):
    mcp.tool()(tool)


if __name__ == "__main__":
    mcp.run()  # stdio transport: the client owns this process
