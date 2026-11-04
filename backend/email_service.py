"""Email channel.

Outbound: replies to Email / Web Form tickets are sent through Gmail when the Gmail channel
is connected (backend/gmail_channel.py sets `sender`), otherwise kept in the outbox. Every
reply, sent or not, is recorded in the outbox table.

Inbound: the Gmail channel's Inbox Agent opens tickets from the mailbox; the dashboard
simulator uses the same receive() intake.
"""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr
from typing import Awaitable, Callable

from backend.ticket_service import TicketService
from core.schemas import TicketContext
from core.store import TicketStore

log = logging.getLogger(__name__)

EMAIL_CHANNELS = {"Email", "Web Form"}
_QUOTE_RE = re.compile(r"\n(On .+wrote:|-{2,} ?Original Message ?-{2,}|From: .+)\n.*", re.DOTALL)


def clean_email_body(body: str) -> str:
    """Drop quoted history and signatures delimiter so agents see only the new message."""
    body = _QUOTE_RE.sub("", body.replace("\r\n", "\n"))
    body = body.split("\n-- \n")[0]
    lines = [ln for ln in body.splitlines() if not ln.startswith(">")]
    return "\n".join(lines).strip()


class EmailService:
    def __init__(self, store: TicketStore) -> None:
        self.store = store
        # (ticket_id, to, subject, body) -> delivers the email; None keeps replies in the outbox.
        self.sender: Callable[[str, str, str, str], Awaitable[None]] | None = None

    # ------------------------------------------------------------------ outbound

    def compose(self, ticket_id: str, subject: str, body: str) -> tuple[str, str]:
        subject_line = f"Re: [{ticket_id}] {subject}" if subject else f"[{ticket_id}] Your Dada Sports support request"
        footer = (
            "\n\n--\nDada Sports Customer Support | support@dadasports.labspace.tech\n"
            f"Please keep [{ticket_id}] in the subject line when you reply."
        )
        return subject_line, body.strip() + footer

    async def send(self, ticket_id: str, to_addr: str, subject: str, body: str) -> str:
        subject_line, full_body = self.compose(ticket_id, subject, body)
        if self.sender is None:
            self.store.add_outbox(ticket_id, to_addr, subject_line, full_body, delivery="stored")
            return "stored"
        try:
            await self.sender(ticket_id, to_addr, subject_line, full_body)
        except Exception as exc:  # keep the reply in the outbox so staff can resend it
            log.exception("Email send failed for %s", ticket_id)
            self.store.add_outbox(ticket_id, to_addr, subject_line, full_body, delivery="failed", error=str(exc)[:300])
            return "failed"
        self.store.add_outbox(ticket_id, to_addr, subject_line, full_body, delivery="sent")
        return "sent"

    async def on_ticket_completed(self, ctx: TicketContext) -> None:
        """Send the agent reply (with the escalation notice when escalated) for email channels."""
        if ctx.channel in EMAIL_CHANNELS and ctx.customer_email and ctx.customer_reply:
            await self.send(ctx.ticket_id, ctx.customer_email, ctx.subject, ctx.customer_reply)

    # ------------------------------------------------------------------ inbound

    async def receive(
        self, tickets: TicketService, from_header: str, subject: str, body: str
    ) -> dict:
        name, address = parseaddr(from_header)
        return await tickets.submit(
            complaint=clean_email_body(body),
            channel="Email",
            customer_name=name or address.split("@")[0],
            customer_email=address,
            subject=subject.strip(),
            # Replies go back to the sending mailbox, so only its owner sees anything history-based.
            identity_verified=True,
        )
