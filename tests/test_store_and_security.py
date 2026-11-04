import csv

from backend.security import RateLimiter, create_token, hash_password, verify_password, verify_token
from core.schemas import ConfidenceReport, EscalationDecision, Passage, TicketContext, TriageResult
from core.store import LOG_COLUMNS


def _processed_ctx(store, **overrides) -> TicketContext:
    created = store.create_ticket("My order is 10 days late.", channel="Email", customer_name="Priya", order_id="ORD-4512")
    ctx = TicketContext(ticket_id=created["ticket_id"], complaint="My order is 10 days late.", customer_name="Priya",
                        order_id="ORD-4512", channel="Email")
    ctx.triage = TriageResult(intent="support", category="Shipping", urgency="High", sentiment="Angry", summary="Late order",
                              product=None, language="English", risk_flags=[], missing_details=[], search_query="late order")
    ctx.passages = [Passage(id="S1", source="shipping_policy.md", section="Delays", text="...", similarity=0.7)]
    ctx.evidence_ids = ctx.cited_ids = ["S1"]
    ctx.reply = "We're sorry for the delay. [S1]"
    ctx.customer_reply = "We're sorry for the delay."
    ctx.confidence = ConfidenceReport(score=0.82, components={"fact_check": 1.0})
    ctx.decision = EscalationDecision(status="Escalated", reasons=["High urgency", "Very angry customer"])
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_ticket_ids_are_sequential(store):
    first = store.create_ticket("one", channel="Chat")
    second = store.create_ticket("two", channel="Chat")
    assert (first["ticket_id"], second["ticket_id"]) == ("TKT-0001", "TKT-0002")
    assert first["access_token"] != second["access_token"]


def test_processed_ticket_writes_required_log_columns(store):
    ctx = _processed_ctx(store)
    store.record_processed_ticket(ctx)

    with store.csv_path.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert reader.fieldnames == LOG_COLUMNS
    row = rows[0]
    assert row["ticket_id"] == ctx.ticket_id
    assert (row["category"], row["urgency"], row["sentiment"]) == ("Shipping", "High", "Angry")
    assert row["kb_sources"] == "shipping_policy.md"
    assert row["status"] == "Escalated"
    assert row["escalation_reason"] == "High urgency; Very angry customer"
    assert store.get(ctx.ticket_id)["desk_status"] == "Escalated"


def test_staff_actions_are_appended_to_audit_log(store):
    ctx = _processed_ctx(store)
    store.record_processed_ticket(ctx)
    store.record_staff_action(ctx.ticket_id, "Replied and closed", "Closed", "support", "Final reply text")
    saved = store.get(ctx.ticket_id)
    assert saved["desk_status"] == "Closed" and saved["status"] == "Escalated" and saved["final_reply"] == "Final reply text"
    store.record_staff_action(ctx.ticket_id, "Reopened", "Escalated", "support", note="customer called")
    assert store.get(ctx.ticket_id)["final_reply"] == "Final reply text"  # reopening keeps the earlier reply
    with store.reviews_csv_path.open(encoding="utf-8-sig") as fh:
        assert [r["action"] for r in csv.DictReader(fh)] == ["Replied and closed", "Reopened"]


def test_events_replay_in_order(store):
    store.create_ticket("x", channel="Chat")
    for kind in ("ticket_queued", "agent_started", "agent_completed"):
        store.add_event("TKT-0001", kind, None, {})
    assert [e["type"] for e in store.events("TKT-0001")] == ["ticket_queued", "agent_started", "agent_completed"]
    assert [e["seq"] for e in store.events("TKT-0001", after_seq=1)] == [2, 3]


def test_password_hashing_and_tokens():
    encoded = hash_password("correct horse battery", iterations=1000)
    assert "$" not in encoded
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong", encoded)
    token, _ = create_token("support", "secret", ttl_seconds=60)
    assert verify_token(token, "secret") == "support"
    assert verify_token(token, "other-secret") is None
    expired, _ = create_token("support", "secret", ttl_seconds=-1)
    assert verify_token(expired, "secret") is None


def test_rate_limiter_blocks_bursts():
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("ip") and limiter.allow("ip")
    assert not limiter.allow("ip")
    assert limiter.allow("other-ip")


def test_customer_history_matches_email_or_order_never_name(store):
    def done(ticket_id, **fields):
        store.update_fields(ticket_id, state="completed", category="Refund", status="Escalated",
                            desk_status="Escalated", summary="Wants a refund", **fields)

    a = store.create_ticket("refund please", channel="Email", customer_name="Priya", customer_email="Priya@Example.com")
    b = store.create_ticket("still no refund", channel="Chat", customer_name="Priya", order_id="ORD-9001")
    c = store.create_ticket("unrelated", channel="Chat", customer_name="Priya")  # same name only
    for t in (a, b, c):
        done(t["ticket_id"])
    current = store.create_ticket("third time!", channel="Email", customer_email="priya@example.com", order_id="ord-9001")

    history = store.customer_history("priya@example.com", "ORD-9001", current["ticket_id"])
    assert [h["ticket_id"] for h in history] == [b["ticket_id"], a["ticket_id"]]  # newest first, name-only excluded
    assert store.customer_history("", "", current["ticket_id"]) == []


def test_ticket_log_keeps_one_row_per_ticket(store):
    first = _processed_ctx(store)
    store.record_processed_ticket(first)
    other = _processed_ctx(store)
    store.record_processed_ticket(other)
    # The same conversation is processed again (a later chat message): its row is replaced, not added.
    first.complaint, first.customer_reply = "ok thanks", "You're welcome!"
    first.decision = EscalationDecision(status="Resolved", reasons=[])
    store.record_processed_ticket(first)
    with store.csv_path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["ticket_id"] for r in rows] == [first.ticket_id, other.ticket_id]
    assert (rows[0]["complaint"], rows[0]["status"]) == ("ok thanks", "Resolved")
