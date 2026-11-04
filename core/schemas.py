"""Shared data contracts for a support ticket as it moves through the agent team.

`TriageResult` is the Customer Query Agent's structured output. `TicketContext` is the
per-ticket record that agent tools read and write, so authoritative values (passages,
confidence, escalation decision) never depend on an LLM re-typing them correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["Shipping", "Refund", "Account", "Product", "Billing", "Other"]
Urgency = Literal["Low", "Medium", "High"]
# "Angry" is the top band and is treated as "very angry" by the escalation policy.
Sentiment = Literal["Positive", "Neutral", "Frustrated", "Angry"]
RiskFlag = Literal["legal_threat", "fraud", "payment_dispute", "safety_hazard"]
Channel = Literal["Chat", "Email", "Web Form"]
Status = Literal["Resolved", "Escalated"]


Intent = Literal["support", "small_talk"]


class TriageResult(BaseModel):
    """What the Customer Query Agent understood about the complaint.

    No field has a default: OpenAI strict structured output requires every field.
    """

    intent: Intent = Field(description="support for a question or problem; small_talk for greetings, thanks, acknowledgements or questions about the assistant")
    category: Category
    urgency: Urgency
    sentiment: Sentiment
    summary: str = Field(description="One-line summary of the customer's problem")
    product: str | None = Field(description="Product or service mentioned, or null")
    language: str = Field(description="Language the customer wrote in, e.g. English, Tamil, Bengali")
    risk_flags: list[RiskFlag] = Field(description="legal_threat / fraud / payment_dispute / safety_hazard, or empty")
    missing_details: list[str] = Field(
        description="Details needed to act on this specific request that the customer did not give "
        "(e.g. 'order ID', 'payment method', 'photos of the damage'), or empty"
    )
    search_query: str = Field(description="Short query for searching the knowledge base")


class EvidenceSelection(BaseModel):
    """The Knowledge Base Agent's decision about which retrieved passages actually answer the issue."""

    selected_ids: list[str] = Field(description="IDs like S1, S3 of passages that are relevant; empty if none")
    no_match: bool = Field(description="True when no retrieved passage addresses the customer's issue")
    rationale: str = Field(description="One sentence explaining the selection")


@dataclass
class Passage:
    """One retrieved knowledge-base chunk."""

    id: str  # display id shown to agents, e.g. "S1"
    source: str  # file name, e.g. "shipping_policy.md"
    section: str  # markdown heading the chunk came from
    text: str
    similarity: float  # cosine similarity to the search query (0..1), used for the relevance gate
    rrf_score: float = 0.0  # fused BM25 + vector rank score, used for ordering
    # Cosine similarity to the customer's request (the Query Agent's English summary), so
    # evidence strength does not depend on how the search query happened to be worded.
    request_similarity: float = 0.0


@dataclass
class ConfidenceReport:
    """Explainable confidence, computed in code from evidence, not self-reported."""

    score: float
    components: dict[str, float]
    notes: list[str] = field(default_factory=list)


@dataclass
class EscalationDecision:
    status: Status
    reasons: list[str]


@dataclass
class TicketContext:
    """Everything known about one ticket; written by tools as the agents work."""

    ticket_id: str
    complaint: str
    customer_name: str = ""
    order_id: str = ""
    customer_email: str = ""
    channel: Channel = "Chat"
    subject: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    # True when the channel proves who the customer is (inbound email from their mailbox, or a
    # ticket raised by staff). Only then may agents see, and replies mention, earlier tickets.
    identity_verified: bool = False
    raised_by_staff: bool = False  # staff may see any order; customers only orders under their email
    # Earlier tickets from the same email address or order ID (newest first), filled in by code.
    prior_tickets: list[dict] = field(default_factory=list)
    history_checked: bool = False
    order_lookup: str = ""  # "" (not looked up) | found | not_verified | not_found

    triage: TriageResult | None = None
    passages: list[Passage] = field(default_factory=list)  # every passage retrieved
    search_queries: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)  # passages the Knowledge Agent approved
    evidence_rationale: str = ""
    no_match: bool = False

    reply: str = ""  # Resolution Agent draft, with [S1] citations
    cited_ids: list[str] = field(default_factory=list)
    confidence: ConfidenceReport | None = None

    agent_recommendation: str = ""  # Log Agent's own judgement, recorded alongside the rules
    decision: EscalationDecision | None = None
    conversation: list[dict] = field(default_factory=list)  # earlier chat turns: {role, text}
    customer_reply: str = ""  # final customer-facing text (citations removed, notice added)
    logged: bool = False

    @property
    def search_count(self) -> int:
        return len(self.search_queries)

    def passage(self, passage_id: str) -> Passage | None:
        return next((p for p in self.passages if p.id == passage_id), None)

    @property
    def evidence(self) -> list[Passage]:
        return [p for i in self.evidence_ids if (p := self.passage(i))]

    @property
    def cited_passages(self) -> list[Passage]:
        return [p for i in self.cited_ids if (p := self.passage(i))]
