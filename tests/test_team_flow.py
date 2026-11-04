"""End-to-end team tests with a scripted model (ReplayChatCompletionClient).

These prove the orchestration itself: agent order, tool handoffs through the ticket context,
termination on LOGGED, escalation and the CSV log row, without calling any real LLM.
"""

from __future__ import annotations

import csv

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from core.schemas import TicketContext
from team import AGENT_ORDER, make_candidate_func, run_ticket
from tests.conftest import ROOT, text, tool_call

MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "structured_output": True,
    "family": "unknown",
}


def triage(**overrides) -> dict:
    data = {
        "intent": "support",
        "category": "Shipping",
        "urgency": "Medium",
        "sentiment": "Frustrated",
        "summary": "Customer's order is one day late.",
        "product": None,
        "language": "English",
        "risk_flags": [],
        "missing_details": [],
        "search_query": "order not arrived by estimated delivery date",
    }
    data.update(overrides)
    return data


async def _run(script, ctx, kb, settings, store, mode="round_robin", orders=None):
    events = []

    async def emit(event_type, agent, data):
        events.append((event_type, agent, data))

    client = ReplayChatCompletionClient(script, model_info=MODEL_INFO)
    meta = await run_ticket(ctx, kb, settings, store.record_processed_ticket, emit, client, mode=mode, orders=orders)
    return events, meta


def _ctx(store, complaint: str) -> TicketContext:
    created = store.create_ticket(complaint, channel="Chat", customer_name="Priya", order_id="ORD-4512")
    return TicketContext(
        ticket_id=created["ticket_id"], complaint=complaint, customer_name="Priya", order_id="ORD-4512"
    )


@pytest.mark.asyncio
async def test_round_robin_resolves_and_logs(kb, test_settings, store):
    ctx = _ctx(store, "Where is my order? It was supposed to arrive yesterday.")
    script = [
        text(triage()),
        tool_call("search_kb", {"query": "order not arrived estimated delivery date", "category": "Shipping"}),
        text({"selected_ids": ["S1"], "no_match": False, "rationale": "Delay policy applies."}),
    ]
    reply = "Hi Priya, sorry your order is late.\nPlease check the tracking link and share your order ID if it is still missing. [S1]"
    script += [
        tool_call("submit_resolution", {"reply": reply}),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]

    events, meta = await _run(script, ctx, kb, test_settings, store)

    speakers = [agent for kind, agent, _ in events if kind == "agent_completed"]
    assert speakers == AGENT_ORDER  # Query -> Knowledge -> Resolution -> Log
    assert "LOGGED" in meta["stop_reason"] or "LOGGED" in "".join(
        d.get("text", "") for k, _, d in events if k == "agent_message"
    )
    assert ctx.triage and ctx.triage.category == "Shipping"
    assert ctx.evidence_ids == ["S1"] and ctx.cited_ids == ["S1"]
    assert ctx.decision is not None and ctx.logged

    with store.csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == ctx.ticket_id
    assert rows[0]["kb_sources"] == ctx.passage("S1").source  # the cited passage's file
    assert "[S1]" not in rows[0]["response"]  # citations stripped for the customer


@pytest.mark.asyncio
async def test_no_match_is_escalated_with_honest_reply(kb, test_settings, store):
    complaint = "Do you sell gift cards for a company of 500 employees with custom branding?"
    ctx = _ctx(store, complaint)
    script = [
        text(triage(category="Other", urgency="Low", sentiment="Neutral", search_query="corporate gift cards")),
        tool_call("search_kb", {"query": "corporate gift cards custom branding", "category": "Other"}),
        text({"selected_ids": [], "no_match": True, "rationale": "No document covers gift cards."}),
        tool_call("submit_resolution", {"reply": "Thank you for asking. I could not find information about gift cards in our policies."}),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]
    await _run(script, ctx, kb, test_settings, store)

    assert ctx.no_match
    assert ctx.decision.status == "Escalated"
    assert "No matching knowledge-base document" in ctx.decision.reasons
    assert ctx.ticket_id in ctx.customer_reply  # escalation notice appended
    saved = store.get(ctx.ticket_id)
    assert saved["status"] == "Escalated" and saved["desk_status"] == "Escalated"


@pytest.mark.asyncio
async def test_legal_threat_escalates_even_without_llm_flags(kb, test_settings, store):
    complaint = "You charged my card three times! Fix this now or I'm contacting my bank and a lawyer."
    ctx = _ctx(store, complaint)
    script = [
        # The scripted triage deliberately misses the risk flags and urgency.
        text(triage(category="Billing", urgency="Medium", sentiment="Frustrated", search_query="charged multiple times")),
        tool_call("search_kb", {"query": "duplicate charge refund", "category": "Billing"}),
        text({"selected_ids": ["S1"], "no_match": False, "rationale": "Duplicate charge policy."}),
        tool_call("submit_resolution", {"reply": "Sorry about this. We will look into the charges. [S1]"}),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]
    await _run(script, ctx, kb, test_settings, store)

    assert ctx.decision.status == "Escalated"
    assert "Legal threat" in ctx.decision.reasons  # keyword safety net
    assert "Payment dispute" in ctx.decision.reasons


@pytest.mark.asyncio
async def test_uncited_reply_is_rejected_once_then_resubmitted(kb, test_settings, store):
    ctx = _ctx(store, "I forgot my password and can't log in.")
    script = [
        text(triage(category="Account", search_query="reset forgotten password")),
        tool_call("search_kb", {"query": "reset forgotten password", "category": "Account"}),
        text({"selected_ids": ["S1"], "no_match": False, "rationale": "Reset steps."}),
        tool_call("submit_resolution", {"reply": "Please use Forgot password on the Login page."}),  # no citation
        tool_call("submit_resolution", {"reply": "Please use Forgot password on the Login page. [S1]"}, "call_2"),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]
    events, _ = await _run(script, ctx, kb, test_settings, store)

    results = [d["content"] for k, a, d in events if k == "tool_result" and a == "resolution_agent"]
    assert results[0].startswith("REJECTED") and results[1].startswith("RESOLUTION SUBMITTED")
    assert ctx.cited_ids == ["S1"] and ctx.confidence.score > 0


@pytest.mark.asyncio
async def test_ticket_is_logged_even_if_log_agent_skips_its_tool(kb, test_settings, store):
    ctx = _ctx(store, "I forgot my password and can't log in.")
    script = [
        text(triage(category="Account", search_query="reset forgotten password")),
        tool_call("search_kb", {"query": "reset forgotten password", "category": "Account"}),
        text({"selected_ids": ["S1"], "no_match": False, "rationale": "Reset steps."}),
        tool_call("submit_resolution", {"reply": "Please use Forgot password on the Login page. [S1]"}),
        text("I think this is fine."),  # Log Agent answers in text instead of calling log_ticket
    ]
    await _run(script, ctx, kb, test_settings, store)

    assert ctx.logged
    assert ctx.decision.status == "Escalated"
    assert any("pipeline ended before logging" in r for r in ctx.decision.reasons)  # system reason, always kept


def test_selector_candidates_enforce_order_and_cap_research():
    from autogen_agentchat.messages import TextMessage

    ctx = TicketContext(ticket_id="TKT-0001", complaint="x")
    candidates = make_candidate_func(ctx)

    def thread(*sources):
        return [TextMessage(source=s, content="...") for s in ("user", *sources)]

    assert candidates(thread()) == ["query_agent"]
    assert candidates(thread("query_agent")) == ["knowledge_agent"]
    assert candidates(thread("query_agent", "knowledge_agent")) == ["resolution_agent"]
    # The one real decision: model may choose to search again or to log.
    assert candidates(thread("query_agent", "knowledge_agent", "resolution_agent")) == ["knowledge_agent", "log_agent"]
    # After one extra search, only logging remains.
    assert candidates(
        thread("query_agent", "knowledge_agent", "resolution_agent", "knowledge_agent", "resolution_agent")
    ) == ["log_agent"]
    # No re-search when the Knowledge Agent already reported no match.
    ctx.no_match = True
    assert candidates(thread("query_agent", "knowledge_agent", "resolution_agent")) == ["log_agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_customer_history_reaches_agents_only_when_identity_is_verified(kb, test_settings, store, verified):
    ctx = _ctx(store, "Still waiting for my refund!")
    ctx.identity_verified = verified
    ctx.prior_tickets = [{"ticket_id": "TKT-0007", "created_at": "2026-11-01 10:00:00", "category": "Refund",
                          "status": "Escalated", "desk_status": "Escalated", "summary": "Asked for a refund"}]
    script = [
        text(triage(category="Refund", search_query="refund timeline")),
        CreateResult(
            finish_reason="function_calls",
            content=[
                FunctionCall(id="c1", name="customer_history", arguments="{}"),
                FunctionCall(id="c2", name="search_kb", arguments='{"query": "refund timeline", "category": "Refund"}'),
            ],
            usage=RequestUsage(prompt_tokens=10, completion_tokens=5),
            cached=False,
        ),
        text({"selected_ids": ["S1"], "no_match": False, "rationale": "Refund timelines."}),
        tool_call("submit_resolution", {"reply": "Refunds follow the timelines in our policy. [S1]"}),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]
    events, _ = await _run(script, ctx, kb, test_settings, store)

    history_result = next(d["content"] for k, a, d in events if k == "tool_result" and d["tool"] == "customer_history")
    evidence_msg = next(d["text"] for k, a, d in events if k == "agent_message" and a == "knowledge_agent")
    if verified:
        assert "TKT-0007" in history_result and "CUSTOMER HISTORY" in evidence_msg
    else:
        assert history_result.startswith("HISTORY UNAVAILABLE") and "TKT-0007" not in evidence_msg
    # The repeat-contact rule uses history on every channel, without disclosing it.
    assert "Repeat contact: TKT-0007 about this is still open" in ctx.decision.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("staff, email, expect_record", [(True, "", True), (False, "intruder@example.com", False)])
async def test_order_lookup_becomes_evidence_only_when_allowed(kb, test_settings, store, staff, email, expect_record):
    from datetime import date

    from core.orders import OrderBook

    orders = OrderBook.load(ROOT / "data" / "orders.json", today=date(2026, 11, 3))
    created = store.create_ticket("Where is my refund for ORD-61299?", channel="Chat", order_id="ORD-61299")
    ctx = TicketContext(ticket_id=created["ticket_id"], complaint="Where is my refund for ORD-61299?",
                        order_id="ORD-61299", customer_email=email, raised_by_staff=staff, identity_verified=staff)
    script = [
        text(triage(category="Refund", search_query="refund timeline", missing_details=["order ID"])),
        tool_call("lookup_order", {"order_id": "ORD-61299"}),
        text({"selected_ids": [], "no_match": True, "rationale": "The order record answers it."}),
        tool_call("submit_resolution", {"reply": "Your ₹3,999 refund was initiated on 30 Oct 2026. [O1]"}),
        tool_call("log_ticket", {"escalate": False, "reason": ""}),
    ]
    events, _ = await _run(script, ctx, kb, test_settings, store, orders=orders)
    evidence_msg = next(d["text"] for k, a, d in events if k == "agent_message" and a == "knowledge_agent")

    if expect_record:
        assert ctx.order_lookup == "found" and "O1" in ctx.evidence_ids
        assert not ctx.no_match  # the order record is evidence even when no policy passage was selected
        assert ctx.triage.missing_details == []  # the order is known, so it is not missing
        assert ctx.cited_ids == ["O1"] and ctx.confidence.components["fact_check"] == 1.0
        # A refund in the record brings the refund-timelines policy into the evidence, chosen by code.
        rule = next(p for p in ctx.passages if (p.source, p.section) == ("refund_policy.md", "Refund timelines"))
        assert rule.id in ctx.evidence_ids
    else:
        assert ctx.order_lookup == "not_verified"
        assert "ARN" not in evidence_msg and "3,999" not in evidence_msg  # no order details leak
        assert "confirm the email" in evidence_msg


def test_small_talk_contact_guard_catches_invented_contact_details():
    from agents.resolution_agent import CONTACT_RE

    for leaked in ["reach our team at info@info.com", "visit www.example.com", "call +91 98765 43210"]:
        assert CONTACT_RE.search(leaked), leaked
    for fine in ["Hello Prakash! How can I help?", "Thanks! Your ticket TKT-0050 is noted."]:
        assert not CONTACT_RE.search(fine), fine
