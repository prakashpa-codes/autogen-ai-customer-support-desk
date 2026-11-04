"""Staff API used by the support dashboard. Every route except login requires a bearer token."""

from __future__ import annotations

import io
import re

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.analytics import summarise
from backend.api_models import (
    EMAIL_PATTERN,
    CloseIn,
    ReopenIn,
    InboundEmailIn,
    KbSearchIn,
    LoginIn,
    ReviseIn,
    StaffTicketIn,
)
from backend.deps import Services, get_services, require_staff
from backend.security import create_token, verify_password
from backend.sse import SSE_HEADERS, sse_stream
from core.store import LOG_COLUMNS

router = APIRouter(prefix="/api/v1", tags=["staff"])

# Large per-ticket fields are left out of list responses.
_HEAVY_FIELDS = {"passages", "evidence", "confidence_detail", "response_annotated", "access_token"}


def _ticket_or_404(services: Services, ticket_id: str) -> dict:
    ticket = services.store.get(ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Ticket {ticket_id} not found")
    return ticket


# ------------------------------------------------------------------------- auth


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, services: Services = Depends(get_services)) -> dict:
    client = request.client.host if request.client else "unknown"
    if not services.limiter.allow(f"login:{client}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many sign-in attempts. Try again in a minute.")
    s = services.settings
    if not s.desk_password_hash:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Staff sign-in is not configured (DESK_PASSWORD_HASH).")
    if body.username != s.desk_username or not verify_password(body.password, s.desk_password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password")
    token, expires = create_token(body.username, services.secret_key, s.token_ttl_hours * 3600)
    return {"token": token, "expires_at": expires, "username": body.username}


@router.get("/auth/me", tags=["auth"])
def whoami(user: str = Depends(require_staff)) -> dict:
    """Validate a stored session token (used by the dashboard to restore a session after a reload)."""
    return {"username": user}


# ------------------------------------------------------------------------- tickets


@router.post("/tickets", status_code=status.HTTP_202_ACCEPTED)
async def create_ticket(body: StaffTicketIn, services: Services = Depends(get_services), _: str = Depends(require_staff)):
    if body.customer_email and not re.match(EMAIL_PATTERN, body.customer_email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid customer email address")
    created = await services.tickets.submit(
        complaint=body.complaint,
        channel=body.channel,
        customer_name=body.customer_name,
        order_id=body.order_id,
        customer_email=body.customer_email,
        subject=body.subject,
        identity_verified=True,  # raised by signed-in staff, who have identified the customer
        raised_by_staff=True,
    )
    return {"ticket_id": created["ticket_id"]}


@router.get("/tickets")
def list_tickets(
    status_: str | None = Query(default=None, alias="status"),
    category: str | None = None,
    channel: str | None = None,
    search: str | None = None,
    limit: int = 500,
    services: Services = Depends(get_services),
    _: str = Depends(require_staff),
) -> list[dict]:
    tickets = services.store.list(status_, category, channel, search, min(limit, 2000))
    return [{k: v for k, v in t.items() if k not in _HEAVY_FIELDS} for t in tickets]


@router.get("/tickets/export")
def export_tickets(fmt: str = "csv", services: Services = Depends(get_services), _: str = Depends(require_staff)):
    tickets = [t for t in services.store.list(limit=100_000) if t.get("status")]
    df = pd.DataFrame(tickets)
    columns = LOG_COLUMNS + ["desk_status", "review_status", "final_reply", "reviewed_by", "reviewed_at"]
    df = df.rename(columns={"created_at": "timestamp"}).reindex(columns=columns) if not df.empty else pd.DataFrame(columns=columns)
    if fmt == "xlsx":
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, sheet_name="Tickets")
        return Response(
            buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=tickets.xlsx"},
        )
    return Response(
        df.to_csv(index=False).encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickets_export.csv"},
    )


@router.get("/logs/tickets.csv")
def ticket_log_file(services: Services = Depends(get_services), _: str = Depends(require_staff)):
    """The ticket log (one row per ticket) written by the Log Agent."""
    if not services.store.csv_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No tickets have been logged yet")
    return FileResponse(services.store.csv_path, media_type="text/csv", filename="tickets.csv")


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, services: Services = Depends(get_services), _: str = Depends(require_staff)) -> dict:
    ticket = _ticket_or_404(services, ticket_id)
    ticket.pop("access_token", None)
    return ticket


@router.get("/tickets/{ticket_id}/timeline")
def ticket_timeline(ticket_id: str, services: Services = Depends(get_services), _: str = Depends(require_staff)) -> list[dict]:
    _ticket_or_404(services, ticket_id)
    return services.store.events(ticket_id)


@router.get("/tickets/{ticket_id}/events")
async def ticket_events(
    ticket_id: str, request: Request, services: Services = Depends(get_services), _: str = Depends(require_staff)
):
    _ticket_or_404(services, ticket_id)
    after = int(request.headers.get("last-event-id") or 0)

    async def events():
        async for event in services.broker.follow(ticket_id, after):
            # Staff see the full event (agent, timestamps, payload); None is a keep-alive.
            yield event if event is None else {"seq": event["seq"], "type": event["type"], "data": event}

    return StreamingResponse(sse_stream(events()), media_type="text/event-stream", headers=SSE_HEADERS)


# ------------------------------------------------------------------------- review


@router.post("/tickets/{ticket_id}/review/revise")
async def revise_reply(
    ticket_id: str, body: ReviseIn, services: Services = Depends(get_services), user: str = Depends(require_staff)
) -> dict:
    _ticket_or_404(services, ticket_id)
    try:
        return await services.review.revise(ticket_id, body.guidance, user)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: str, body: CloseIn, services: Services = Depends(get_services), user: str = Depends(require_staff)
) -> dict:
    ticket = _ticket_or_404(services, ticket_id)
    if ticket.get("desk_status") == "Closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "The ticket is already closed")
    try:
        return await services.review.close(ticket_id, user, body.reply, body.note)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/tickets/{ticket_id}/reopen")
def reopen_ticket(
    ticket_id: str, body: ReopenIn, services: Services = Depends(get_services), user: str = Depends(require_staff)
) -> dict:
    ticket = _ticket_or_404(services, ticket_id)
    if ticket.get("desk_status") == "Escalated":
        raise HTTPException(status.HTTP_409_CONFLICT, "The ticket is already waiting for a person")
    return services.review.reopen(ticket_id, user, body.note)


# ------------------------------------------------------------------------- analytics, email, kb


@router.get("/analytics")
def analytics(services: Services = Depends(get_services), _: str = Depends(require_staff)) -> dict:
    return summarise(services.store.list(limit=100_000))


@router.get("/email/outbox")
def outbox(services: Services = Depends(get_services), _: str = Depends(require_staff)) -> dict:
    gmail = services.gmail.status() if services.gmail else {"connected": False, "last_error": "", "activity": []}
    return {"gmail": gmail, "messages": services.store.outbox()}


@router.post("/email/inbound", status_code=status.HTTP_202_ACCEPTED)
async def inbound_email(body: InboundEmailIn, services: Services = Depends(get_services), _: str = Depends(require_staff)):
    """Turn a pasted email into an Email-channel ticket (the inbound simulator)."""
    created = await services.email.receive(services.tickets, body.from_header, body.subject, body.body)
    return {"ticket_id": created["ticket_id"]}


@router.get("/kb/documents")
def kb_documents(services: Services = Depends(get_services), _: str = Depends(require_staff)) -> list[dict]:
    docs: dict[str, dict] = {}
    for chunk in services.kb.chunks:
        doc = docs.setdefault(chunk.source, {"source": chunk.source, "title": chunk.title, "sections": 0, "words": 0})
        doc["sections"] += 1
        doc["words"] += len(chunk.text.split())
    return sorted(docs.values(), key=lambda d: d["source"])


@router.post("/kb/search")
def kb_search(body: KbSearchIn, services: Services = Depends(get_services), _: str = Depends(require_staff)) -> list[dict]:
    return [
        {
            "source": r.chunk.source,
            "section": r.chunk.section,
            "similarity": round(r.similarity, 3),
            "rrf_score": round(r.rrf_score, 4),
            "dense_rank": r.dense_rank,
            "bm25_rank": r.bm25_rank,
            "above_gate": r.similarity >= services.settings.min_relevance,
            "text": r.chunk.text,
        }
        for r in services.kb.search(body.query, body.category, body.k)
    ]


@router.post("/kb/reindex")
def kb_reindex(services: Services = Depends(get_services), _: str = Depends(require_staff)) -> dict:
    services.kb.build()
    return {"chunks": len(services.kb.chunks), "documents": len(services.kb.sources())}
