"""Log Agent: applies the escalation policy, records the ticket and hands complex cases to humans.

Tool: log_ticket(escalate, reason). The deterministic policy (core/escalation.py) always runs;
the agent adds its own judgement on top, which can escalate a ticket but never un-escalate one.
The tool result starts with LOGGED, which ends the team run (TextMentionTermination).
"""

from __future__ import annotations

from typing import Callable

from autogen_core.models import ChatCompletionClient

from agents.base import RecordingAssistantAgent, RequiredToolClient, strict_tool
from agents.templates import compose_customer_reply
from core.escalation import decide_escalation
from core.schemas import TicketContext

NAME = "log_agent"
TERMINATION_WORD = "LOGGED"
DESCRIPTION = (
    "Log Agent: records every ticket to the ticket log and decides whether it must be escalated to human support."
)

SYSTEM_MESSAGE = """You are the Log Agent at Dada Sports. You are the last step for every ticket.
Your single job is to record the ticket by calling log_ticket exactly once.

The escalation policy is applied automatically inside log_ticket. A ticket is always escalated when urgency
is High, the customer is very angry, no matching document was found, legal threats, fraud, payment
disputes or safety hazards are involved, the Resolution Agent's confidence is below the threshold, or the
customer's history shows a repeat contact about the same kind of issue.

Your judgement is the extra safety layer. Set escalate=true (with a short reason) only when a human is
needed for something the rules do not capture, for example:
- a possible safety risk to the customer (injury, smoke, electrical fault);
- the reply cannot actually be acted on because it needs a staff decision that the policy does not grant;
- the customer says this is a repeated contact about an unresolved problem that the history does not show.
Otherwise set escalate=false and reason to "". Never restate the automatic rules as your reason. Do not escalate just because the reply asks the customer
to share an order ID or follow documented steps - that is a normal resolution."""


def create_log_agent(
    model_client: ChatCompletionClient,
    ctx: TicketContext,
    confidence_threshold: float,
    record: Callable[[TicketContext], None],
) -> RecordingAssistantAgent:
    async def log_ticket(escalate: bool, reason: str) -> str:
        """Apply the escalation policy and append this ticket to the ticket log.

        Args:
            escalate: True if, in your judgement, a human must handle this ticket beyond the automatic rules.
            reason: Short reason for your judgement; empty string when escalate is false.
        """
        if ctx.logged:
            return f"{TERMINATION_WORD} {ctx.ticket_id} (already recorded)"
        ctx.agent_recommendation = f"escalate: {reason}" if escalate else "no additional escalation"
        ctx.decision = decide_escalation(
            triage=ctx.triage,
            complaint=" ".join([ctx.subject, ctx.complaint]),
            no_match=ctx.no_match,
            confidence=ctx.confidence,
            threshold=confidence_threshold,
            agent_escalate=escalate,
            agent_reason=reason,
            prior_tickets=ctx.prior_tickets,
            now=ctx.created_at,
        )
        ctx.customer_reply = compose_customer_reply(ctx)
        record(ctx)
        ctx.logged = True
        reasons = "; ".join(ctx.decision.reasons) or "none"
        return f"{TERMINATION_WORD} {ctx.ticket_id} | status: {ctx.decision.status} | escalation reasons: {reasons}"

    return RecordingAssistantAgent(
        name=NAME,
        description=DESCRIPTION,
        system_message=SYSTEM_MESSAGE,
        model_client=RequiredToolClient(model_client),  # the turn must end in this tool call
        tools=[strict_tool(log_ticket)],
        max_tool_iterations=1,
        reflect_on_tool_use=False,
    )
