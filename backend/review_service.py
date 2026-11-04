"""Staff work on tickets: AI revision of a reply, closing and reopening.

* revise: a small AutoGen team where a UserProxyAgent represents the support lead. Its input
  function returns the guidance typed in the dashboard; the Resolution Agent then redrafts
  the reply, still restricted to the ticket's evidence pack and still scored for confidence.
* close: the ticket is done. With a reply (email channels), the reply becomes the final reply
  and is emailed in the customer's thread; without one, only an internal note is kept.
* reopen: a closed or resolved ticket goes back to Escalated, waiting for a person.
Every action is recorded in logs/reviews.csv.
"""

from __future__ import annotations

from autogen_agentchat.agents import UserProxyAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.teams import RoundRobinGroupChat

from agents.resolution_agent import create_resolution_agent
from backend.email_service import EMAIL_CHANNELS, EmailService
from backend.ticket_service import context_from_ticket
from config import Settings
from core.confidence import strip_citations
from core.store import TicketStore


class ReviewService:
    def __init__(self, settings: Settings, store: TicketStore, email_service: EmailService, model_client_factory) -> None:
        self.settings = settings
        self.store = store
        self.email = email_service
        self.model_client_factory = model_client_factory

    async def revise(self, ticket_id: str, guidance: str, reviewer: str) -> dict:
        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        ctx = context_from_ticket(ticket)
        original_reply = ctx.reply

        async def supervisor_input(prompt: str, cancellation_token=None) -> str:
            return guidance

        support_lead = UserProxyAgent(
            "support_lead",
            description="Senior support lead reviewing an escalated ticket.",
            input_func=supervisor_input,
        )
        client = self.model_client_factory()
        try:
            resolution = create_resolution_agent(client, ctx, self.settings.min_relevance)
            team = RoundRobinGroupChat([support_lead, resolution], termination_condition=MaxMessageTermination(3))
            evidence = "\n\n".join(f"[{p.id}] {p.source} > {p.section}\n{p.text}" for p in ctx.evidence) or "NO_MATCH"
            task = (
                f"Escalated ticket {ticket_id} needs a revised reply.\n"
                f"Channel: {ctx.channel}. Customer name: {ctx.customer_name or 'unknown'}. Language: "
                f"{ctx.triage.language if ctx.triage else 'English'}.\n"
                f"Customer message:\n<<<\n{ctx.complaint}\n>>>\n\n"
                f"EVIDENCE PACK\n{evidence}\n\nCurrent draft:\n{original_reply}\n\n"
                "The support lead will give guidance next. Follow it only where the evidence supports it."
            )
            await team.run(task=task)
        finally:
            await client.close()

        if ctx.reply == original_reply:
            raise RuntimeError("The Resolution Agent did not submit a revised reply.")
        return {
            "draft": ctx.reply,
            "customer_draft": strip_citations(ctx.reply),
            "confidence": ctx.confidence.score if ctx.confidence else 0.0,
            "confidence_detail": {
                "components": ctx.confidence.components if ctx.confidence else {},
                "notes": ctx.confidence.notes if ctx.confidence else [],
            },
            "reviewer": reviewer,
        }

    async def close(self, ticket_id: str, reviewer: str, reply: str = "", note: str = "") -> dict:
        ticket = self.store.get(ticket_id)
        if ticket is None:
            raise KeyError(ticket_id)
        delivery = None
        if reply.strip():
            if ticket["channel"] not in EMAIL_CHANNELS or not ticket.get("customer_email"):
                raise ValueError("This ticket has no email address to send a reply to.")
            delivery = await self.email.send(ticket_id, ticket["customer_email"], ticket.get("subject") or "", reply)
        action = "Replied and closed" if reply.strip() else "Closed"
        self.store.record_staff_action(ticket_id, action, "Closed", reviewer, reply.strip(), note)
        return {"ticket_id": ticket_id, "desk_status": "Closed", "action": action, "email_delivery": delivery}

    def reopen(self, ticket_id: str, reviewer: str, note: str = "") -> dict:
        if self.store.get(ticket_id) is None:
            raise KeyError(ticket_id)
        self.store.record_staff_action(ticket_id, "Reopened", "Escalated", reviewer, note=note)
        return {"ticket_id": ticket_id, "desk_status": "Escalated", "action": "Reopened"}
