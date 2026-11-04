"""Wires the four agents into one AutoGen team and runs a ticket through it.

Two orchestration modes (TEAM_MODE):

* round_robin - RoundRobinGroupChat, fixed order Query -> Knowledge -> Resolution -> Log.
* selector    - SelectorGroupChat. A candidate function enforces the same order, except at one
                real decision point: after the Resolution Agent, the model chooses between the
                Log Agent and one extra Knowledge Agent search when the evidence was weak.

Both stop on TextMentionTermination("LOGGED") from the Log Agent, with MaxMessageTermination
as a safety net. Run a ticket from the terminal for debugging:

    python team.py "The blender I received is broken. Can I get a replacement?"
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from typing import Any, Awaitable, Callable, Sequence

from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    SelectorEvent,
    SelectSpeakerEvent,
    StructuredMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat
from autogen_core.models import ChatCompletionClient

from agents import knowledge_agent, log_agent, query_agent, resolution_agent
from agents.base import FallbackClient
from agents.knowledge_agent import create_knowledge_agent
from agents.log_agent import TERMINATION_WORD, create_log_agent
from agents.query_agent import create_query_agent
from agents.resolution_agent import create_resolution_agent
from agents.templates import compose_customer_reply
from config import Settings
from core.escalation import decide_escalation
from core.orders import OrderBook
from core.retrieval import KnowledgeBase
from core.schemas import EvidenceSelection, TicketContext, TriageResult

# AutoGen's structured-output parsing triggers a harmless Pydantic serializer warning on every call.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

AGENT_ORDER = [query_agent.NAME, knowledge_agent.NAME, resolution_agent.NAME, log_agent.NAME]
MAX_RESEARCH = 1  # selector mode: at most one extra Knowledge Agent search

EmitFn = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]
RecordFn = Callable[[TicketContext], None]

SELECTOR_PROMPT = """You are routing a customer support ticket between specialist agents.

{roles}

Conversation so far:
{history}

The Resolution Agent has just submitted a reply with an evidence-based confidence score.
Choose knowledge_agent ONLY if the confidence is below {threshold} because facts were missing or unverified
and a different search is likely to find them. Choose log_agent if the reply is well supported, or if the
Knowledge Agent already reported NO_MATCH (searching again will not help).
Select one of {participants}. Reply with the agent name only."""


def format_task(ctx: TicketContext) -> str:
    lines = [f"New support ticket {ctx.ticket_id}", f"Channel: {ctx.channel}"]
    if ctx.customer_name:
        lines.append(f"Customer name: {ctx.customer_name}")
    if ctx.order_id:
        lines.append(f"Order ID: {ctx.order_id}")
    if ctx.subject:
        lines.append(f"Email subject: {ctx.subject}")
    if ctx.conversation:
        lines.append("Earlier in this chat (oldest first):")
        lines += [f"- {t['role']}: {t['text'][:400]}" for t in ctx.conversation[-6:]]
    lines += ["Customer message:", "<<<", ctx.complaint.strip(), ">>>"]
    return "\n".join(lines)


def make_candidate_func(ctx: TicketContext) -> Callable[[Sequence[BaseAgentEvent | BaseChatMessage]], list[str]]:
    """Enforce the mandatory order; leave only the post-resolution choice to the model."""

    def candidates(thread: Sequence[BaseAgentEvent | BaseChatMessage]) -> list[str]:
        speakers = [m.source for m in thread if isinstance(m, BaseChatMessage) and m.source in AGENT_ORDER]
        last = speakers[-1] if speakers else None
        if last is None:
            return [query_agent.NAME]
        if last == query_agent.NAME:
            return [knowledge_agent.NAME]
        if last == knowledge_agent.NAME:
            return [resolution_agent.NAME]
        if last == resolution_agent.NAME:
            researched = speakers.count(knowledge_agent.NAME) > MAX_RESEARCH
            if researched or ctx.no_match:
                return [log_agent.NAME]
            return [knowledge_agent.NAME, log_agent.NAME]
        return [log_agent.NAME]

    return candidates


def build_team(
    ctx: TicketContext,
    model_client: ChatCompletionClient,
    kb: KnowledgeBase,
    settings: Settings,
    record: RecordFn,
    mode: str | None = None,
    orders: OrderBook | None = None,
) -> RoundRobinGroupChat | SelectorGroupChat:
    participants = [
        create_query_agent(model_client, ctx),
        create_knowledge_agent(model_client, ctx, kb, settings.min_relevance, settings.top_k, orders),
        create_resolution_agent(model_client, ctx, settings.min_relevance),
        create_log_agent(model_client, ctx, settings.confidence_threshold, record),
    ]
    mode = mode or settings.team_mode
    # Structured outputs travel through the team runtime, so their message types must be registered.
    message_types = [StructuredMessage[TriageResult], StructuredMessage[EvidenceSelection]]
    # Safety net: the task message + one message per agent turn (+2 for the optional re-search).
    max_messages = 1 + len(AGENT_ORDER) + (2 * MAX_RESEARCH if mode == "selector" else 0)
    termination = TextMentionTermination(TERMINATION_WORD, sources=[log_agent.NAME]) | MaxMessageTermination(
        max_messages
    )
    if mode == "selector":
        return SelectorGroupChat(
            participants,
            model_client=model_client,
            termination_condition=termination,
            candidate_func=make_candidate_func(ctx),
            selector_prompt=SELECTOR_PROMPT.replace("{threshold}", f"{settings.confidence_threshold:.2f}"),
            allow_repeated_speaker=False,
            emit_team_events=True,
            custom_message_types=message_types,
        )
    return RoundRobinGroupChat(
        participants, termination_condition=termination, emit_team_events=True, custom_message_types=message_types
    )


def _snapshot(agent: str, ctx: TicketContext) -> dict[str, Any]:
    """Structured result of an agent's turn, read from the shared ticket context."""
    if agent == query_agent.NAME and ctx.triage:
        return {"triage": ctx.triage.model_dump()}
    if agent == knowledge_agent.NAME:
        return {
            "no_match": ctx.no_match,
            "rationale": ctx.evidence_rationale,
            "queries": ctx.search_queries,
            "evidence": [
                {"id": p.id, "source": p.source, "section": p.section, "similarity": round(p.similarity, 3), "text": p.text}
                for p in ctx.evidence
            ],
            "order_lookup": ctx.order_lookup,
            "history_checked": ctx.history_checked,
            "identity_verified": ctx.identity_verified,
            # Staff may always see prior tickets; whether agents saw them is shown by identity_verified.
            "prior_tickets": ctx.prior_tickets,
        }
    if agent == resolution_agent.NAME:
        c = ctx.confidence
        return {
            "reply": ctx.reply,
            "cited_ids": ctx.cited_ids,
            "confidence": {"score": c.score, "components": c.components, "notes": c.notes} if c else None,
        }
    if agent == log_agent.NAME and ctx.decision:
        return {
            "status": ctx.decision.status,
            "reasons": ctx.decision.reasons,
            "agent_recommendation": ctx.agent_recommendation,
            "customer_reply": ctx.customer_reply,
        }
    return {}


async def run_ticket(
    ctx: TicketContext,
    kb: KnowledgeBase,
    settings: Settings,
    record: RecordFn,
    emit: EmitFn,
    model_client: ChatCompletionClient,
    mode: str | None = None,
    orders: OrderBook | None = None,
) -> dict[str, Any]:
    """Run one ticket through the team, emitting UI-friendly events. Returns run metadata."""
    mode = mode or settings.team_mode
    team = build_team(ctx, model_client, kb, settings, record, mode, orders)
    started = time.perf_counter()
    stop_reason = ""

    async for item in team.run_stream(task=format_task(ctx)):
        if isinstance(item, TaskResult):
            stop_reason = item.stop_reason or ""
        elif isinstance(item, SelectSpeakerEvent):
            for name in item.content:
                await emit("agent_started", name, {})
        elif isinstance(item, SelectorEvent):
            await emit("routing", "orchestrator", {"text": item.to_text()})
        elif isinstance(item, ToolCallRequestEvent):
            for call in item.content:
                try:
                    arguments = json.loads(call.arguments)
                except (TypeError, ValueError):
                    arguments = {"raw": call.arguments}
                await emit("tool_call", item.source, {"tool": call.name, "arguments": arguments})
        elif isinstance(item, ToolCallExecutionEvent):
            for result in item.content:
                await emit(
                    "tool_result",
                    item.source,
                    {"tool": result.name, "content": result.content[:6000], "is_error": bool(result.is_error)},
                )
        elif isinstance(item, BaseChatMessage) and item.source in AGENT_ORDER:
            text = item.to_text() if not isinstance(item, StructuredMessage) else item.to_model_text()
            await emit("agent_message", item.source, {"text": text})
            await emit("agent_completed", item.source, _snapshot(item.source, ctx))

    if not ctx.logged:
        # Fail-safe: every ticket is recorded (and escalated), even if the Log Agent never called its tool.
        decision = decide_escalation(
            triage=ctx.triage,
            complaint=" ".join([ctx.subject, ctx.complaint]),
            no_match=ctx.no_match,
            confidence=ctx.confidence,
            threshold=settings.confidence_threshold,
            prior_tickets=ctx.prior_tickets,
            now=ctx.created_at,
        )
        decision.reasons.append("Log Agent did not record the ticket: pipeline ended before logging")
        decision.status = "Escalated"
        ctx.decision = decision
        ctx.customer_reply = compose_customer_reply(ctx)
        record(ctx)
        ctx.logged = True
        await emit("agent_completed", log_agent.NAME, _snapshot(log_agent.NAME, ctx))

    usage = model_client.total_usage()
    return {
        "team_mode": mode,
        "stop_reason": stop_reason,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
    }


def create_model_client(settings: Settings) -> ChatCompletionClient:
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set. Copy .env.example to .env and add your key.")
    primary = OpenAIChatCompletionClient(model=settings.model, api_key=settings.openai_api_key, temperature=0.1)
    if not settings.gemini_api_key:
        return primary
    # Gemini through its OpenAI-compatible endpoint. AutoGen 0.7.5 predates the Gemini 3 models,
    # so the endpoint and the model's capabilities are given explicitly.
    fallback = OpenAIChatCompletionClient(
        model=settings.gemini_model,
        api_key=settings.gemini_api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        temperature=0.1,
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": "unknown",
            "multiple_system_messages": False,  # AutoGen merges system messages into one
        },
    )
    return FallbackClient(primary, fallback)


async def _cli(complaint: str) -> None:
    from config import settings
    from core.retrieval import OpenAIEmbedder
    from core.store import TicketStore

    kb = KnowledgeBase(settings.kb_dir, OpenAIEmbedder(settings.embedding_model, settings.openai_api_key), settings.index_dir).build()
    store = TicketStore(settings.db_path, settings.log_dir)
    created = store.create_ticket(complaint, channel="Chat", customer_name="CLI")
    ctx = TicketContext(ticket_id=created["ticket_id"], complaint=complaint, customer_name="CLI")

    async def emit(event_type: str, agent: str | None, data: dict[str, Any]) -> None:
        if event_type in {"agent_message", "tool_call"}:
            print(f"\n--- {agent} [{event_type}] ---\n{data.get('text') or json.dumps(data, ensure_ascii=False)}")

    client = create_model_client(settings)
    try:
        meta = await run_ticket(ctx, kb, settings, store.record_processed_ticket, emit, client)
    finally:
        await client.close()
    print(f"\n=== {ctx.ticket_id}: {ctx.decision.status if ctx.decision else '?'} | {meta}")
    print(ctx.customer_reply)


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252, which has no ₹
    asyncio.run(_cli(" ".join(sys.argv[1:]) or "Where is my order? It was supposed to arrive yesterday."))
