"""Response templates per channel, and the customer-facing reply assembly.

The Resolution Agent writes the body following the channel template. The final
customer reply is assembled in code: citations are removed and, for escalated tickets,
a fixed escalation notice is added. The SLA wording mirrors about_dada_sports.md.
"""

from __future__ import annotations

from core.confidence import strip_citations
from core.schemas import TicketContext

CHANNEL_TEMPLATES = {
    "Chat": (
        "Chat reply, at most 120 words, short lines:\n"
        "  1. Greeting using the customer's first name if known, plus one sentence acknowledging the issue "
        "in a tone that matches their mood.\n"
        "  2. The answer or numbered steps. Every line that states a policy fact ends with its citation, e.g. [S2].\n"
        "  3. What happens next or what the customer should do now, with the timeline from the evidence [Sx]; if details are missing, ask for them here.\n"
        "  4. One short closing line."
    ),
    "Email": (
        "Email body, at most 200 words:\n"
        "  1. 'Dear <first name>,' (or 'Dear Customer,'), then one or two sentences acknowledging the issue.\n"
        "  2. The answer or numbered steps. Every line that states a policy fact ends with its citation, e.g. [S2].\n"
        "  3. A 'Next steps' line with the timeline from the evidence [Sx]; if details are missing, ask for them here.\n"
        "  4. Sign off exactly as: 'Warm regards,' then 'Dada Sports Customer Support'.\n"
        "  Do not write a subject line."
    ),
}
CHANNEL_TEMPLATES["Web Form"] = CHANNEL_TEMPLATES["Email"]


def escalation_notice(ctx: TicketContext) -> str:
    triage = ctx.triage
    urgent = triage is not None and (triage.urgency == "High" or bool(triage.risk_flags))
    window = "4 business hours" if urgent else "24 hours"
    return (
        f"Your ticket {ctx.ticket_id} has been passed to our senior support team. "
        f"A specialist will contact you within {window}, and you can quote this ticket number in any reply."
    )


def compose_customer_reply(ctx: TicketContext) -> str:
    body = strip_citations(ctx.reply).strip()
    if ctx.decision is not None and ctx.decision.status == "Escalated":
        notice = escalation_notice(ctx)
        return f"{body}\n\n{notice}" if body else notice
    return body
