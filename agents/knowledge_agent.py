"""Knowledge Base Agent: searches company documents and selects only the relevant passages.

Tools: search_kb(query, category) -> hybrid BM25 + FAISS search over knowledge_base/.
       customer_history() -> the customer's earlier tickets, only when their identity is verified.
       lookup_order(order_id) -> the order record [O1], only for staff or when the email matches.
Output: an Evidence Pack. The LLM decides which passages are relevant (EvidenceSelection);
the pack itself is rendered by code from the index, so passage text and file names reach the
Resolution Agent exactly as written in the documents.
"""

from __future__ import annotations

import asyncio

from autogen_agentchat.base import Response
from autogen_agentchat.messages import StructuredMessage, TextMessage
from autogen_core.models import ChatCompletionClient

from agents.base import RecordingAssistantAgent, strict_tool
from agents.query_agent import ORDER_REF_RE
from core.orders import OrderBook
from core.retrieval import CATEGORY_SOURCES, KnowledgeBase
from core.schemas import EvidenceSelection, Passage, TicketContext

NAME = "knowledge_agent"
DESCRIPTION = (
    "Knowledge Base Agent: searches Dada Sports policies, FAQs and the catalogue and returns "
    "only the passages that answer the issue, with their source file."
)

SYSTEM_MESSAGE = """You are the Knowledge Base Agent at Dada Sports.
Your single job is to find the company documents that answer the customer's issue. You never write to the customer.

Steps:
1. Read the Customer Query Agent's triage (category, summary, search query).
2. Call customer_history and search_kb (a focused query and the ticket category) together. If the request
   is about the customer's own order (delivery, return, exchange, refund, cancellation or a payment), also call
   lookup_order: with the order ID when there is one (ORD-...), otherwise with an empty string to find it by
   the customer's email. An order record shows what has happened; still search the policy that explains
   what happens next (refund timelines, delivery delays, return windows).
   customer_history shows earlier tickets or says history is unavailable. lookup_order returns the order
   record (status, items, payment, delivery, return and refund) or says why it cannot be shown.
3. Product listings (product_catalogue.md) only describe items. If the customer asks about returns,
   exchanges, refunds, warranty, delivery, payments or discounts and the results do not include the policy
   section that governs it, call search_kb once more for that policy (for example "size exchange policy"
   with category Refund, or "bulk order discount" with category Other).
4. The order record is included automatically, but it only says what has happened: also select the policy
   passage that tells the customer what happens next (for example refund timelines by payment method, or the
   steps for a late parcel). Select only passages that directly address the customer's actual request. Do not select passages that
   merely share words with it. Prefer 1-4 passages.
5. If no passage addresses the request (for example, a product or service Dada Sports does not offer),
   set no_match to true and select nothing. An honest "not found" is better than a loose match.
   A missing order ID or order record is NOT a no-match: if a policy covers the topic, select it.

If you are called again after the Resolution Agent, it means its draft lacked evidence. Read its notes,
search for the missing facts, and return a new selection."""


def history_lines(ctx: TicketContext) -> str:
    """Earlier tickets as one line each: what the customer contacted us about and what happened."""
    lines = []
    for t in ctx.prior_tickets:
        outcome = t.get("desk_status") or t.get("status") or "Processing"
        lines.append(f"- {t['ticket_id']} on {t['created_at'][:10]}: {t.get('category') or '-'}, {outcome}. {t.get('summary') or ''}".rstrip())
    return "\n".join(lines)


def _governing_policy(ctx: TicketContext, min_relevance: float) -> list[str]:
    """With an order record, make sure the best retrieved passage from the ticket category's own policy
    documents is in the evidence: the record says what happened, the policy says what happens next."""
    preferred = CATEGORY_SOURCES.get(ctx.triage.category if ctx.triage else "", set()) - {"product_catalogue.md"}
    candidates = [
        p for p in ctx.passages
        if p.id.startswith("S") and p.source in preferred and p.similarity >= min_relevance
    ]
    best = max(candidates, key=lambda p: max(p.similarity, p.request_similarity), default=None)
    return [best.id] if best else []


REFUND_TIMELINES = ("refund_policy.md", "Refund timelines")

ORDER_LOOKUP_NOTES = {
    "not_verified": "ORDER LOOKUP: the order exists but its details cannot be shown, because the email on this "
                    "ticket does not match the order. Ask the customer to confirm the email address used for the order.",
    "not_found": "ORDER LOOKUP: no order with this ID was found. Ask the customer to check the order ID "
                 "(it starts with ORD- and appears in the confirmation email).",
}


def create_knowledge_agent(
    model_client: ChatCompletionClient,
    ctx: TicketContext,
    kb: KnowledgeBase,
    min_relevance: float,
    top_k: int,
    orders: OrderBook | None = None,
) -> RecordingAssistantAgent:
    attached: list[str] = []  # policy passages code attaches to an order record (always in the evidence)

    def _register(source: str, section: str, text: str, similarity: float, rrf: float) -> Passage:
        """Give each distinct passage a stable ID (S1, S2, ...) across repeated searches."""
        for existing in ctx.passages:
            if existing.text == text and existing.source == source:
                existing.similarity = max(existing.similarity, similarity)
                return existing
        number = sum(1 for p in ctx.passages if p.id.startswith("S")) + 1
        passage = Passage(
            id=f"S{number}", source=source, section=section, text=text, similarity=similarity, rrf_score=rrf
        )
        ctx.passages.append(passage)
        return passage

    async def search_kb(query: str, category: str) -> str:
        """Search the Dada Sports knowledge base (policies, FAQs, catalogue, past resolutions).

        Args:
            query: What to look for, in English.
            category: Ticket category (Shipping, Refund, Account, Product, Billing or Other); boosts matching documents.
        """
        ctx.search_queries.append(query)
        results = await asyncio.to_thread(kb.search, query, category, top_k)
        # Multi-query retrieval: also search with the customer's request itself, so recall does not
        # depend on one well-worded query (e.g. "refund status" misses the refund timelines).
        request = ctx.triage.summary if ctx.triage else ""
        if request and request.strip().lower() != query.strip().lower():
            merged = {r.chunk.key: r for r in results}
            for r in await asyncio.to_thread(kb.search, request, category, top_k):
                if r.chunk.key not in merged or r.similarity > merged[r.chunk.key].similarity:
                    merged[r.chunk.key] = r
            results = sorted(merged.values(), key=lambda r: r.similarity, reverse=True)
        usable = [r for r in results if r.similarity >= min_relevance]
        rejected = len(results) - len(usable)
        if not usable:
            return (
                f"NO_MATCH: no passage reached the relevance threshold ({min_relevance:.2f}) for '{query}'. "
                f"Best similarity was {max((r.similarity for r in results), default=0):.2f}."
            )
        # How well each result matches the customer's actual request, independent of query wording.
        request = ctx.triage.summary if ctx.triage else ""
        request_sims = (
            await asyncio.to_thread(kb.similarity_to, request, [r.chunk for r in usable]) if request else [0.0] * len(usable)
        )
        lines = [f"Results for '{query}' (category {category}):"]
        for r, request_sim in zip(usable, request_sims):
            p = _register(r.chunk.source, r.chunk.section, r.chunk.text, r.similarity, r.rrf_score)
            p.request_similarity = max(p.request_similarity, request_sim)
            lines.append(f"\n[{p.id}] {p.source} > {p.section} (similarity {p.similarity:.2f})\n{p.text}")
        if rejected:
            lines.append(f"\n({rejected} weaker results below the relevance threshold were discarded.)")
        return "\n".join(lines)

    async def customer_history() -> str:
        """Look up this customer's earlier support tickets (matched by email address or order ID)."""
        ctx.history_checked = True
        if not ctx.identity_verified:
            return (
                "HISTORY UNAVAILABLE: this channel does not verify who the customer is, so earlier tickets "
                "must not be used or mentioned."
            )
        if not ctx.prior_tickets:
            return "No earlier tickets from this customer in the last 90 days."
        return "Earlier tickets from this customer (newest first):\n" + history_lines(ctx)

    async def lookup_order(order_id: str) -> str:
        """Look up a Dada Sports order: status, items, payment, delivery, return and refund.

        Args:
            order_id: The order ID (ORD-...). Use an empty string to list this customer's orders by email.
        """
        if orders is None:
            return "Order lookup is not available."
        order_id = order_id.strip() or ctx.order_id
        if not order_id:
            found = orders.orders_for_email(ctx.customer_email) if ctx.identity_verified and ctx.customer_email else []
            if len(found) > 1:
                return f"Orders under this customer's email: {', '.join(found)}. Call lookup_order with the one they mean."
            if not found:
                return "No order ID was given and none is linked to a verified email. Ask the customer for the order ID."
            order_id = found[0]  # exactly one order under the verified email
        result = orders.lookup(order_id, ctx.customer_email, staff=ctx.raised_by_staff)
        ctx.order_lookup = result.status
        if result.status != "found":
            return ORDER_LOOKUP_NOTES[result.status]
        existing = next((p for p in ctx.passages if p.source == f"order {result.order_id}"), None)
        if existing is None:
            number = sum(1 for p in ctx.passages if p.id.startswith("O")) + 1
            # An exact record, not a search hit: it is fully relevant by definition.
            existing = Passage(id=f"O{number}", source=f"order {result.order_id}", section="Order record",
                               text=result.record, similarity=1.0, request_similarity=1.0)
            ctx.passages.append(existing)
        if not ctx.order_id:
            ctx.order_id = result.order_id
        if ctx.triage:  # the order is known now, so it is no longer a missing detail
            ctx.triage.missing_details = [d for d in ctx.triage.missing_details if not ORDER_REF_RE.search(d)]
        reply = f"[{existing.id}] ORDER RECORD {result.order_id}\n{result.record}"
        # The record says what happened to a refund; the policy says when the money arrives. A search
        # can miss the timelines table (matching a past case instead), so code attaches it.
        if "Refund:" in result.record:
            rule = next((c for c in kb.chunks if (c.source, c.section) == REFUND_TIMELINES), None)
            if rule is not None:
                request = ctx.triage.summary if ctx.triage else rule.text
                similarity = (await asyncio.to_thread(kb.similarity_to, request, [rule]))[0]
                p = _register(rule.source, rule.section, rule.text, similarity, 0.0)
                attached.append(p.id)
                reply += f"\n\nREFUND RULE for this order's payment method:\n[{p.id}] {p.source} > {p.section}\n{p.text}"
        return reply

    def publish_evidence(response: Response) -> Response | None:
        message = response.chat_message
        if not (isinstance(message, StructuredMessage) and isinstance(message.content, EvidenceSelection)):
            # The model answered in free text: treat as no usable selection.
            ctx.evidence_ids, ctx.no_match = [], True
            ctx.evidence_rationale = "Knowledge Agent did not return a structured selection."
        else:
            selection = message.content
            # The relevance gate is authoritative: only gated passages can be used as evidence.
            valid = [i for i in dict.fromkeys(selection.selected_ids) if (p := ctx.passage(i)) and p.similarity >= min_relevance]
            order_ids = [p.id for p in ctx.passages if p.id.startswith("O")]
            if order_ids:
                valid = list(dict.fromkeys(valid + _governing_policy(ctx, min_relevance) + attached))
            ctx.evidence_ids = list(dict.fromkeys(order_ids + valid))
            # Selected passages win over the flag: "no_match" means the documents do not cover the
            # topic, not that order-specific details are missing.
            ctx.no_match = not ctx.evidence_ids
            ctx.evidence_rationale = selection.rationale

        if ctx.triage and ctx.triage.intent == "small_talk":
            # Greetings and acknowledgements need no policy: an empty pack stops the reply repeating earlier answers.
            ctx.evidence_ids, ctx.no_match = [], False
            content = "SMALL TALK: no policy evidence is needed. Reply briefly and do not repeat earlier answers."
        elif ctx.no_match:
            content = (
                "NO_MATCH: none of the Dada Sports documents cover this request.\n"
                f"Searched: {'; '.join(ctx.search_queries) or '-'}\n"
                f"Rationale: {ctx.evidence_rationale}"
            )
        else:
            blocks = [
                f"[{p.id}] {p.source} > {p.section} (similarity {p.similarity:.2f})\n{p.text}" for p in ctx.evidence
            ]
            content = (
                f"EVIDENCE PACK ({len(blocks)} passage{'s' if len(blocks) != 1 else ''})\n"
                f"Rationale: {ctx.evidence_rationale}\n\n" + "\n\n".join(blocks)
            )
        if ctx.order_lookup in ORDER_LOOKUP_NOTES:
            content += "\n\n" + ORDER_LOOKUP_NOTES[ctx.order_lookup]
        if ctx.history_checked and ctx.identity_verified and ctx.prior_tickets:
            content += "\n\nCUSTOMER HISTORY (verified customer, newest first)\n" + history_lines(ctx)
        return Response(
            chat_message=TextMessage(source=NAME, content=content, models_usage=message.models_usage),
            inner_messages=response.inner_messages,
        )

    return RecordingAssistantAgent(
        name=NAME,
        description=DESCRIPTION,
        system_message=SYSTEM_MESSAGE,
        model_client=model_client,
        tools=[strict_tool(search_kb), strict_tool(customer_history), strict_tool(lookup_order)],
        max_tool_iterations=3,
        output_content_type=EvidenceSelection,
        on_response=publish_evidence,
    )
