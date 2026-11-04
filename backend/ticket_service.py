"""Ticket intake and processing.

submit() stores the ticket and returns immediately (HTTP 202). A fixed pool of async workers
takes tickets from a queue and runs the agent team, so a burst of tickets waits in line
instead of overloading the model API. Scaling beyond one host would move this queue to
Redis with separate worker processes, behind the same interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from agents.templates import compose_customer_reply
from backend.events import EventBroker
from config import Settings
from core.schemas import EscalationDecision, Passage, TicketContext, TriageResult
from core.store import TicketStore

log = logging.getLogger(__name__)

# A pipeline runs one ticket and returns run metadata. The real one runs the AutoGen team;
# tests inject a fake one.
EmitFn = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]
Pipeline = Callable[[TicketContext, EmitFn], Awaitable[dict[str, Any]]]
CompletionHook = Callable[[TicketContext], Awaitable[None]]


ORDER_ID_RE = re.compile(r"\bORD-\d{4,}\b", re.IGNORECASE)


def context_from_ticket(ticket: dict[str, Any], include_results: bool = True) -> TicketContext:
    """Rebuild a TicketContext from a stored ticket.

    include_results=False gives a fresh context for processing; True restores the agents'
    results (triage, evidence, reply) for human review.
    """
    ctx = TicketContext(
        ticket_id=ticket["ticket_id"],
        complaint=ticket["complaint"],
        customer_name=ticket.get("customer_name") or "",
        order_id=ticket.get("order_id") or "",
        customer_email=ticket.get("customer_email") or "",
        channel=ticket.get("channel") or "Chat",
        subject=ticket.get("subject") or "",
        created_at=datetime.strptime(ticket["created_at"], "%Y-%m-%d %H:%M:%S"),
        identity_verified=bool(ticket.get("identity_verified")),
        raised_by_staff=bool(ticket.get("raised_by_staff")),
    )
    if not include_results:
        return ctx
    if ticket.get("category"):
        risk = [r for r in (ticket.get("risk_flags") or "").split(",") if r]  # stored comma-separated
        ctx.triage = TriageResult(
            intent="support",
            category=ticket["category"],
            urgency=ticket["urgency"],
            sentiment=ticket["sentiment"],
            summary=ticket.get("summary") or "",
            product=ticket.get("product"),
            language=ticket.get("language") or "English",
            risk_flags=risk,
            missing_details=[d for d in (ticket.get("missing_details") or "").split("; ") if d],
            search_query="",
        )
    ctx.passages = [Passage(**p) for p in (ticket.get("passages") or [])]
    evidence = ticket.get("evidence") or {}
    ctx.evidence_ids = evidence.get("ids", [])
    ctx.no_match = evidence.get("no_match", False)
    ctx.evidence_rationale = evidence.get("rationale", "")
    ctx.search_queries = evidence.get("queries", [])
    ctx.reply = ticket.get("response_annotated") or ""
    ctx.cited_ids = evidence.get("cited_ids", [])
    if ticket.get("status"):
        ctx.decision = EscalationDecision(
            status=ticket["status"], reasons=[r for r in (ticket.get("escalation_reason") or "").split("; ") if r]
        )
    ctx.customer_reply = ticket.get("response") or ""
    return ctx


def _earlier_turns(ticket: dict[str, Any]) -> list[dict]:
    """Chat turns before the message being processed now (the last customer turn)."""
    turns = json.loads(ticket.get("conversation") or "[]")
    return turns[:-1] if turns and turns[-1]["role"] == "customer" else turns


class TicketService:
    def __init__(
        self,
        store: TicketStore,
        broker: EventBroker,
        settings: Settings,
        pipeline: Pipeline,
        on_completed: CompletionHook | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.settings = settings
        self.pipeline = pipeline
        self.on_completed = on_completed
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(n), name=f"ticket-worker-{n}")
            for n in range(self.settings.max_concurrent_tickets)
        ]

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------ intake

    async def submit(
        self,
        complaint: str,
        channel: str,
        customer_name: str = "",
        order_id: str = "",
        customer_email: str = "",
        subject: str = "",
        identity_verified: bool = False,
        raised_by_staff: bool = False,
    ) -> dict[str, Any]:
        """Queue a ticket. identity_verified=True only when the channel proves who the customer is."""
        if not order_id.strip():
            # Customers often put the order number in the message instead of the order field.
            found = ORDER_ID_RE.search(f"{subject} {complaint}")
            order_id = found.group(0) if found else ""
        created = self.store.create_ticket(
            complaint=complaint.strip(),
            channel=channel,
            customer_name=customer_name.strip(),
            order_id=order_id.strip().upper(),
            customer_email=customer_email.strip(),
            subject=subject.strip(),
            identity_verified=identity_verified,
            raised_by_staff=raised_by_staff,
        )
        await self.broker.publish(
            created["ticket_id"], "ticket_queued", None, {"channel": channel, "queue_position": self.queue_depth + 1}
        )
        self._queue.put_nowait(created["ticket_id"])
        return created

    async def continue_conversation(self, ticket_id: str, message: str) -> int:
        """Add a customer message to an existing chat ticket and process it again.

        Returns the last event sequence number before this turn, so the client follows only new events.
        """
        events = self.store.events(ticket_id)
        last_seq = events[-1]["seq"] if events else 0
        self.store.add_turn(ticket_id, "customer", message.strip())
        await self.broker.publish(ticket_id, "ticket_queued", None, {"channel": "Chat", "queue_position": self.queue_depth + 1})
        self._queue.put_nowait(ticket_id)
        return last_seq

    # ------------------------------------------------------------------ processing

    async def _worker(self, number: int) -> None:
        while True:
            ticket_id = await self._queue.get()
            try:
                await self.process(ticket_id)
            except Exception:  # never let one ticket kill the worker
                log.exception("Worker %d failed on %s", number, ticket_id)
            finally:
                self._queue.task_done()

    async def process(self, ticket_id: str) -> None:
        ticket = self.store.get(ticket_id)
        if ticket is None or ticket["state"] == "completed":
            return
        ctx = context_from_ticket(ticket, include_results=False)
        ctx.conversation = _earlier_turns(ticket)
        # Earlier tickets from the same email or order ID. Always used by the repeat-contact rule;
        # shown to the agents only when the customer's identity is verified (see customer_history tool).
        ctx.prior_tickets = self.store.customer_history(ctx.customer_email, ctx.order_id, ticket_id)
        self.store.set_state(ticket_id, "processing")
        await self.broker.publish(ticket_id, "ticket_started", None, {})

        async def emit(event_type: str, agent: str | None, data: dict[str, Any]) -> None:
            await self.broker.publish(ticket_id, event_type, agent, data)

        try:
            meta = await self.pipeline(ctx, emit)
            self.store.update_fields(
                ticket_id,
                team_mode=meta.get("team_mode"),
                model=self.settings.model,
                prompt_tokens=meta.get("prompt_tokens"),
                completion_tokens=meta.get("completion_tokens"),
                duration_ms=meta.get("duration_ms"),
            )
            self.store.set_state(ticket_id, "completed")
        except Exception as exc:
            log.exception("Ticket %s failed", ticket_id)
            if not ctx.logged:
                # Every ticket must reach the log: record it as escalated with the error.
                ctx.decision = EscalationDecision(status="Escalated", reasons=[f"Processing error: {type(exc).__name__}"])
                ctx.customer_reply = compose_customer_reply(ctx)
                self.store.record_processed_ticket(ctx)
                ctx.logged = True
            self.store.set_state(ticket_id, "failed", error=str(exc)[:500])
            await self.broker.publish(ticket_id, "ticket_failed", None, {"error": type(exc).__name__, "customer_reply": ctx.customer_reply})
            return

        if ctx.channel == "Chat" and ctx.customer_reply:
            self.store.add_turn(ticket_id, "assistant", ctx.customer_reply)
        if self.on_completed is not None:
            try:
                await self.on_completed(ctx)
            except Exception:
                log.exception("Completion hook failed for %s", ticket_id)

        await self.broker.publish(
            ticket_id,
            "ticket_completed",
            None,
            {
                "status": ctx.decision.status if ctx.decision else "Escalated",
                "reasons": ctx.decision.reasons if ctx.decision else [],
                "customer_reply": ctx.customer_reply,
                "confidence": ctx.confidence.score if ctx.confidence else 0.0,
                "kb_sources": sorted({p.source for p in ctx.cited_passages}),
                "meta": json.loads(json.dumps(meta, default=str)),
            },
        )
