"""Resolution Agent: writes the customer reply using only the Evidence Pack, then submits it.

Tool: submit_resolution(reply) validates the [S#] citations and computes the confidence score
in code (see core/confidence.py). The tool result, not the model's opinion, is what the team sees.
"""

from __future__ import annotations

import re

from autogen_agentchat.base import Response
from autogen_agentchat.messages import TextMessage
from autogen_core.models import ChatCompletionClient

from agents.base import RecordingAssistantAgent, RequiredToolClient, strict_tool
from agents.templates import CHANNEL_TEMPLATES
from core.confidence import CITATION_RE, assess_confidence
from core.schemas import TicketContext

NAME = "resolution_agent"
DESCRIPTION = (
    "Resolution Agent: writes a clear, polite reply grounded only in the Knowledge Agent's evidence, "
    "and reports an evidence-based confidence score."
)

SYSTEM_MESSAGE = """You are the Resolution Agent at Dada Sports, an Indian cricket and sports retailer.
Your single job is to write the reply to the customer, using ONLY facts from the Knowledge Base Agent's
latest EVIDENCE PACK, and submit it with the submit_resolution tool.

Grounding rules:
- Use only facts stated in the evidence passages. Never invent policies, prices, time limits, compensation,
  phone numbers or steps. If a detail is not in the evidence, do not state it.
- End every line that states a fact from the evidence with its passage citation, e.g. [S2] or [S1][S3].
- Copy numbers exactly as they appear in the evidence (₹ amounts, days, hours).
- Never use vague time words such as "shortly", "soon" or "a few days". Give the exact timeline from the
  evidence (for example the refund time for the customer's payment method), or say plainly that you cannot
  give a timeline.
- Passages from resolved_cases.md describe how one past customer was helped. Use them for how to handle
  the case, but take timelines, amounts and rules from the policy passages; never present a past case's
  outcome as what will happen for this customer.
- Do not promise outcomes the evidence does not describe, and do not say the ticket has been escalated;
  the system adds any escalation notice itself.
- If the Knowledge Base Agent reported NO_MATCH, write a short, honest reply: thank the customer, say plainly
  that you could not find information about this request in Dada Sports' policies, and do not guess or
  suggest alternatives that are not in the evidence. Use no citations in that case.

Small talk (the triage intent is small_talk): reply in one or two short, friendly sentences and use no
citations. To "who are you", introduce yourself as the Dada Sports support assistant who helps with orders,
delivery, returns, payments, accounts and products. To thanks or "ok", acknowledge briefly and offer more
help. Never repeat information already given earlier in the chat. Small talk states no facts: no email
addresses, links, phone numbers, prices or policies, and never treat an address the customer wrote as ours.
For requests outside customer support (sending emails for them, writing code, general questions), say
politely that you can only help with Dada Sports orders, products and policies.

Acting on the customer's specific case:
- You know about the customer's order only through an ORDER RECORD [O1] in the evidence pack. When there is
  one, use its facts (status, dates, amounts, payment, return and refund progress), cite them like policy
  facts, and combine them with the policy: for example, compare the delivery date with the return window.
- Without an ORDER RECORD you cannot see orders, payments or deliveries: never say or imply that a refund,
  replacement, cancellation, pickup or investigation has started, or that you checked an order.
- If the pack says ORDER LOOKUP could not show the order, ask for what it asks for (the email used for the
  order, or the correct order ID) and say nothing else about the order.
- If the triage lists missing details (for example the order ID), ask for them clearly, and say what happens
  once Dada Sports receives them. An order ID is not missing when the pack has an ORDER RECORD.
- If the evidence pack contains CUSTOMER HISTORY and an earlier ticket is about the same issue, acknowledge
  it in your first paragraph by ticket number and date (for example "I can see you wrote to us about this
  refund on 2 Nov (TKT-0019), and I'm sorry it is still not sorted"). Mention only the ticket number, date
  and topic. Without a CUSTOMER HISTORY section, never mention earlier contact.

Style:
- Write in the customer's language (given in the triage). Keep citations as [S#].
- Warm, specific and concise. Acknowledge the customer's feelings when they are frustrated or angry.
- Use the customer's first name if it is given.

Format for this ticket's channel:
{template}

Always finish by calling submit_resolution exactly once with the complete reply."""


SUBMITTED = "RESOLUTION SUBMITTED"
MIN_COVERAGE = 0.6  # below this, the first submission is sent back once to add citations
# Small talk is not fact-checked, so it may not carry contact details (they could be invented or
# copied from the customer's message and presented as ours).
CONTACT_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.|\+?\d[\d\s-]{7,}\d")
SAFE_SMALL_TALK = (
    "I'm the Dada Sports support assistant. I can help with orders, delivery, returns, payments, "
    "accounts and products. What can I help you with?"
)


def resolution_summary(ctx: TicketContext) -> str:
    """The Resolution Agent's handoff message, rendered from the validated submission."""
    c = ctx.confidence
    if c is None:
        return "NO RESOLUTION: the Resolution Agent did not submit a reply."
    parts = ", ".join(f"{k.replace('_', ' ')} {v:.2f}" for k, v in c.components.items())
    return (
        f"{SUBMITTED} - confidence {c.score:.2f} ({parts})\n"
        f"Sources cited: {', '.join(f'{p.id} {p.source}' for p in ctx.cited_passages) or 'none'}\n"
        f"Checks: {' '.join(c.notes)}\n\n"
        f"Reply:\n{ctx.reply}"
    )


def create_resolution_agent(
    model_client: ChatCompletionClient,
    ctx: TicketContext,
    min_relevance: float,
) -> RecordingAssistantAgent:
    attempts = {"rejected": 0}

    async def submit_resolution(reply: str) -> str:
        """Submit the customer reply. Citations like [S1] are validated and a confidence score is computed.

        Args:
            reply: The complete customer-facing reply, with [S#] citations after factual lines.
        """
        cited = list(dict.fromkeys(CITATION_RE.findall(reply)))
        evidence = set(ctx.evidence_ids)
        valid = [i for i in cited if i in evidence]
        invalid = [i for i in cited if i not in evidence]

        # Numbers that come from the ticket itself (order ID, ticket numbers, dates of earlier
        # tickets) are not policy facts, so the fact check ignores them.
        history_refs = " ".join(f"{t['ticket_id']} {t['created_at']}" for t in ctx.prior_tickets) if ctx.identity_verified else ""
        customer_text = " ".join([ctx.complaint, ctx.order_id, ctx.customer_name, ctx.subject, ctx.ticket_id, history_refs])
        report = assess_confidence(
            reply=reply.strip(),
            cited=[p for i in valid if (p := ctx.passage(i))],
            retrieved=ctx.passages,
            category=ctx.triage.category if ctx.triage else None,
            customer_text=customer_text,
            min_relevance=min_relevance,
            invalid_ids=invalid,
        )

        # One chance to fix citations when evidence exists: uncited statements cannot be verified.
        coverage = report.components["citation_coverage"]
        small_talk = ctx.triage is not None and ctx.triage.intent == "small_talk"
        if small_talk and CONTACT_RE.search(reply):
            if attempts["rejected"] == 0:
                attempts["rejected"] += 1
                return ("REJECTED: small talk must not contain email addresses, links or phone numbers. "
                        "Resubmit a short friendly reply without them.")
            reply = SAFE_SMALL_TALK
        if not small_talk and evidence and not ctx.no_match and attempts["rejected"] == 0 and (not valid or coverage < MIN_COVERAGE):
            attempts["rejected"] += 1
            problem = "cites no evidence passage" if not valid else f"cites passages on only {coverage:.0%} of its factual lines"
            return (
                f"REJECTED: the reply {problem}. Resubmit the same reply with the [S#] citation at the end of "
                f"every line that states a fact, including list items. Available passages: {', '.join(sorted(evidence))}."
            )

        ctx.reply = reply.strip()
        ctx.cited_ids = valid
        ctx.confidence = report
        return resolution_summary(ctx)

    def publish(response: Response) -> Response:
        if not ctx.reply:
            # Safety net: a reply written as plain text is kept but unvalidated (0 confidence -> escalation).
            text = getattr(response.chat_message, "content", "")
            if isinstance(text, str) and text.strip() and text.strip() != "Done.":
                ctx.reply = CITATION_RE.sub("", text).strip()
        return Response(
            chat_message=TextMessage(source=NAME, content=resolution_summary(ctx), models_usage=response.chat_message.models_usage),
            inner_messages=response.inner_messages,
        )

    template = CHANNEL_TEMPLATES.get(ctx.channel, CHANNEL_TEMPLATES["Chat"])
    return RecordingAssistantAgent(
        name=NAME,
        description=DESCRIPTION,
        system_message=SYSTEM_MESSAGE.format(template=template),
        # Must call the tool; allows one resubmission if the first reply was rejected.
        model_client=RequiredToolClient(model_client, done_marker=SUBMITTED),
        tools=[strict_tool(submit_resolution)],
        max_tool_iterations=2,
        reflect_on_tool_use=False,
        on_response=publish,
    )
