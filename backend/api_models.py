"""Request bodies for the HTTP API, validated by FastAPI/Pydantic."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class PublicTicketIn(BaseModel):
    """Raised from the storefront chat widget or contact form."""

    channel: Literal["Chat", "Web Form"] = "Chat"
    message: str = Field(min_length=2, max_length=4000)
    name: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=200)
    order_id: str = Field(default="", max_length=40)
    subject: str = Field(default="", max_length=200)
    # Continue an existing chat conversation (same ticket) instead of opening a new one.
    ticket_id: str = Field(default="", max_length=20)
    token: str = Field(default="", max_length=64)


class StaffTicketIn(BaseModel):
    """Raised by staff from the support dashboard (any channel)."""

    channel: Literal["Chat", "Email", "Web Form"] = "Chat"
    complaint: str = Field(min_length=5, max_length=4000)
    customer_name: str = Field(default="", max_length=100)
    customer_email: str = Field(default="", max_length=200)
    order_id: str = Field(default="", max_length=40)
    subject: str = Field(default="", max_length=200)


class InboundEmailIn(BaseModel):
    from_header: str = Field(min_length=3, max_length=300, description='e.g. "Priya Sen <priya@example.com>"')
    subject: str = Field(default="", max_length=200)
    body: str = Field(min_length=5, max_length=8000)


class LoginIn(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


class ReviseIn(BaseModel):
    guidance: str = Field(min_length=3, max_length=1000)


class CloseIn(BaseModel):
    reply: str = Field(default="", max_length=6000)  # empty: close without emailing the customer
    note: str = Field(default="", max_length=500)


class ReopenIn(BaseModel):
    note: str = Field(default="", max_length=500)


class KbSearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    category: str | None = None
    k: int = Field(default=5, ge=1, le=15)
