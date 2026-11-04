"""HTTP API tests with a fake pipeline (no LLM): intake, access control, SSE, export, review."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security import hash_password
from core.schemas import ConfidenceReport, EscalationDecision, TriageResult

PASSWORD = "dada-test-password"


def make_app(settings, kb):
    """Build the real app with a fake pipeline that stands in for the agent team."""
    app = None

    async def fake_pipeline(ctx, emit):
        return await _fake_run(ctx, emit, app.state.services.store)

    app = create_app(settings=settings, kb=kb, pipeline=fake_pipeline, model_client_factory=lambda: None)
    return app


async def _fake_run(ctx, emit, store):
    """Escalates anything mentioning a lawyer; resolves everything else."""
    for agent in ("query_agent", "knowledge_agent", "resolution_agent", "log_agent"):
        await emit("agent_started", agent, {})
        await emit("agent_completed", agent, {})
    angry = "lawyer" in ctx.complaint
    ctx.triage = TriageResult(intent="support", category="Billing" if angry else "Shipping", urgency="High" if angry else "Medium",
                              sentiment="Angry" if angry else "Neutral", summary="s", product=None,
                              language="English", risk_flags=["legal_threat"] if angry else [], missing_details=[], search_query="q")
    ctx.reply = "Thanks for reaching out."
    ctx.customer_reply = "Thanks for reaching out."
    ctx.confidence = ConfidenceReport(score=0.9, components={})
    ctx.decision = EscalationDecision(status="Escalated" if angry else "Resolved", reasons=["Legal threat"] if angry else [])
    store.record_processed_ticket(ctx)
    ctx.logged = True
    return {"team_mode": "round_robin", "duration_ms": 5, "prompt_tokens": 1, "completion_tokens": 1}


@pytest.fixture
def client(kb, test_settings):
    settings = replace(test_settings, desk_password_hash=hash_password(PASSWORD, iterations=1000), secret_key="k",
                       public_rate_limit=100)
    with TestClient(make_app(settings, kb)) as c:
        yield c


def _login(client) -> dict:
    token = client.post("/api/v1/auth/login", json={"username": "support", "password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _wait_done(client, headers, ticket_id):
    for _ in range(100):
        if client.get(f"/api/v1/tickets/{ticket_id}", headers=headers).json()["state"] == "completed":
            return
        time.sleep(0.02)
    raise AssertionError("ticket did not complete")


def test_staff_routes_require_login(client):
    assert client.get("/api/v1/tickets").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "support", "password": "nope"}).status_code == 401


def test_public_ticket_flow_with_customer_safe_events(client):
    r = client.post("/api/v1/public/tickets", json={"message": "Where is my order? It is late.", "name": "Priya"})
    assert r.status_code == 202
    body = r.json()
    ticket_id, token = body["ticket_id"], body["access_token"]

    with client.stream("GET", body["events_url"]) as stream:
        frames = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith("data: ")]
    assert frames[-1]["type"] == "completed" and frames[-1]["status"] == "Resolved"
    assert any(f.get("stage") == "Checking our policies" for f in frames)
    assert all("passages" not in f and "confidence" not in f for f in frames)  # no internals leak

    assert client.get(f"/api/v1/public/tickets/{ticket_id}", params={"token": "wrong"}).status_code == 404
    assert client.get(f"/api/v1/public/tickets/{ticket_id}", params={"token": token}).json()["status"] == "Resolved"


def test_escalated_ticket_close_reopen_and_export(client):
    headers = _login(client)
    r = client.post("/api/v1/tickets", headers=headers,
                    json={"complaint": "Fix this or I call my lawyer", "channel": "Email",
                          "customer_email": "priya@example.com", "customer_name": "Priya"})
    ticket_id = r.json()["ticket_id"]
    _wait_done(client, headers, ticket_id)

    waiting = client.get("/api/v1/tickets", headers=headers, params={"status": "Escalated"}).json()
    assert [t["ticket_id"] for t in waiting] == [ticket_id]

    closed = client.post(f"/api/v1/tickets/{ticket_id}/close", headers=headers,
                         json={"reply": "Our payments lead will call you today.", "note": "called bank"}).json()
    assert (closed["desk_status"], closed["action"]) == ("Closed", "Replied and closed")
    assert closed["email_delivery"] == "stored"  # Gmail off in tests -> outbox
    assert len(client.get("/api/v1/email/outbox", headers=headers).json()["messages"]) >= 1
    assert client.get("/api/v1/tickets", headers=headers, params={"status": "Escalated"}).json() == []
    assert client.post(f"/api/v1/tickets/{ticket_id}/close", headers=headers, json={}).status_code == 409

    reopened = client.post(f"/api/v1/tickets/{ticket_id}/reopen", headers=headers, json={"note": "customer called back"}).json()
    assert reopened["desk_status"] == "Escalated"
    ticket = client.get(f"/api/v1/tickets/{ticket_id}", headers=headers).json()
    assert ticket["status"] == "Escalated" and ticket["review_status"] == "Reopened"  # AI decision is kept

    csv_export = client.get("/api/v1/tickets/export", headers=headers, params={"fmt": "csv"})
    assert ticket_id in csv_export.text and "escalation_reason" in csv_export.text
    xlsx = client.get("/api/v1/tickets/export", headers=headers, params={"fmt": "xlsx"})
    assert xlsx.content[:2] == b"PK"  # zip container = valid xlsx

    stats = client.get("/api/v1/analytics", headers=headers).json()
    assert stats["escalated"] == 1 and stats["escalation_reasons"] == {"Legal threat": 1}


def test_public_validation_and_rate_limit(kb, test_settings):
    with TestClient(make_app(replace(test_settings, public_rate_limit=1, secret_key="k"), kb)) as c:
        assert c.post("/api/v1/public/tickets", json={"message": "a"}).status_code == 422  # too short
        assert c.post("/api/v1/public/tickets", json={"message": "Where is my parcel?"}).status_code == 202
        assert c.post("/api/v1/public/tickets", json={"message": "Where is my parcel?"}).status_code == 429


def test_session_token_can_be_revalidated(client):
    headers = _login(client)
    assert client.get("/api/v1/auth/me", headers=headers).json() == {"username": "support"}
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_order_id_in_message_is_captured_at_intake(client):
    headers = _login(client)
    ticket_id = client.post("/api/v1/tickets", headers=headers,
                            json={"complaint": "Where is my parcel for ord-48213? It is late."}).json()["ticket_id"]
    assert client.get(f"/api/v1/tickets/{ticket_id}", headers=headers).json()["order_id"] == "ORD-48213"


def test_chat_conversation_continues_the_same_ticket(client):
    first = client.post("/api/v1/public/tickets", json={"message": "hi there"}).json()
    with client.stream("GET", first["events_url"]) as s:
        list(s.iter_lines())
    second = client.post("/api/v1/public/tickets", json={
        "message": "Where is my order? It is late.", "ticket_id": first["ticket_id"], "token": first["access_token"]}).json()
    assert second["ticket_id"] == first["ticket_id"]  # one ticket per conversation
    with client.stream("GET", second["events_url"]) as s:
        frames = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
    assert frames[0]["type"] == "ticket_queued" and frames[-1]["type"] == "completed"  # only the new turn
    t = client.get(f"/api/v1/tickets/{first['ticket_id']}", headers=_login(client)).json()
    turns = json.loads(t["conversation"])
    assert [x["role"] for x in turns] == ["customer", "assistant", "customer", "assistant"]
    assert client.post("/api/v1/public/tickets", json={
        "message": "hello", "ticket_id": first["ticket_id"], "token": "wrong"}).status_code == 404
