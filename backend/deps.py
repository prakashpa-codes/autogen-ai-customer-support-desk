"""Service container and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.email_service import EmailService
from backend.events import EventBroker
from backend.review_service import ReviewService
from backend.security import RateLimiter, verify_token
from backend.ticket_service import TicketService
from config import Settings
from core.retrieval import KnowledgeBase
from core.store import TicketStore


@dataclass
class Services:
    settings: Settings
    store: TicketStore
    kb: KnowledgeBase
    broker: EventBroker
    tickets: TicketService
    email: EmailService
    review: ReviewService
    limiter: RateLimiter
    secret_key: str
    catalogue: dict[str, Any]
    model_client_factory: Callable
    gmail: Any = None  # GmailChannel when the Gmail MCP channel is connected


def get_services(request: Request) -> Services:
    return request.app.state.services


_bearer = HTTPBearer(auto_error=False)


def require_staff(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    services: Services = Depends(get_services),
) -> str:
    username = verify_token(credentials.credentials, services.secret_key) if credentials else None
    if username is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    return username
