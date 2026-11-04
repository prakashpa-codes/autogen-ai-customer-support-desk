"""Customer-facing API used by the storefront: raise a ticket and follow its progress.

A customer can only see their own ticket: every read needs the access token returned at
creation. Internal details (prompts, passages, scores) are never exposed here.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from backend.api_models import EMAIL_PATTERN, PublicTicketIn
from backend.deps import Services, get_services
from backend.sse import SSE_HEADERS, sse_stream

router = APIRouter(prefix="/api/v1/public", tags=["public"])

STAGES = {
    "query_agent": "Understanding your request",
    "knowledge_agent": "Checking our policies",
    "resolution_agent": "Writing your answer",
    "log_agent": "Finalising your ticket",
}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _authorised_ticket(services: Services, ticket_id: str, token: str) -> dict:
    ticket = services.store.get(ticket_id)
    if ticket is None or ticket["access_token"] != token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return ticket


def public_event(event: dict) -> dict | None:
    """Translate an internal event into a customer-safe progress update."""
    kind, agent, data = event["type"], event.get("agent"), event["data"]
    if kind == "agent_started" and agent in STAGES:
        return {"type": "stage", "stage": STAGES[agent], "state": "active"}
    if kind == "agent_completed" and agent in STAGES:
        return {"type": "stage", "stage": STAGES[agent], "state": "done"}
    if kind == "ticket_completed":
        return {"type": "completed", "status": data["status"], "reply": data["customer_reply"]}
    if kind == "ticket_failed":
        return {"type": "completed", "status": "Escalated", "reply": data.get("customer_reply", "")}
    if kind in {"ticket_queued", "ticket_started"}:
        return {"type": kind}
    return None


@router.get("/catalogue")
def catalogue(services: Services = Depends(get_services)) -> dict:
    return services.catalogue


@router.post("/tickets", status_code=status.HTTP_202_ACCEPTED)
async def create_ticket(body: PublicTicketIn, request: Request, services: Services = Depends(get_services)) -> dict:
    if not services.limiter.allow(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Please wait a minute and try again.")
    if body.email and not re.match(EMAIL_PATTERN, body.email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Please enter a valid email address.")
    if body.ticket_id and body.channel == "Chat":
        ticket = _authorised_ticket(services, body.ticket_id, body.token)
        after = await services.tickets.continue_conversation(ticket["ticket_id"], body.message)
        return {
            "ticket_id": ticket["ticket_id"],
            "access_token": body.token,
            "events_url": f"/api/v1/public/tickets/{ticket['ticket_id']}/events?token={body.token}&after={after}",
        }
    if body.channel == "Web Form" and not body.email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "An email address is required for support tickets.")
    created = await services.tickets.submit(
        complaint=body.message,
        channel=body.channel,
        customer_name=body.name,
        order_id=body.order_id,
        customer_email=body.email,
        subject=body.subject,
    )
    return {
        "ticket_id": created["ticket_id"],
        "access_token": created["access_token"],
        "events_url": f"/api/v1/public/tickets/{created['ticket_id']}/events?token={created['access_token']}",
    }


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, token: str, services: Services = Depends(get_services)) -> dict:
    t = _authorised_ticket(services, ticket_id, token)
    return {
        "ticket_id": t["ticket_id"],
        "state": t["state"],
        "status": t["status"],
        "reply": t["final_reply"] or t["response"],
        "created_at": t["created_at"],
    }


@router.get("/tickets/{ticket_id}/events")
async def ticket_events(ticket_id: str, token: str, request: Request, after: int = 0, services: Services = Depends(get_services)):
    _authorised_ticket(services, ticket_id, token)
    after = int(request.headers.get("last-event-id") or after)

    async def events():
        async for event in services.broker.follow(ticket_id, after):
            if event is None:
                yield None
                continue
            mapped = public_event(event)
            if mapped is not None:
                yield {"seq": event["seq"], "type": mapped["type"], "data": mapped}

    return StreamingResponse(sse_stream(events()), media_type="text/event-stream", headers=SSE_HEADERS)
