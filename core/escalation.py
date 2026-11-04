"""Escalation policy: the rules that decide whether a ticket goes to a human.

A ticket is Escalated when ANY of these is true:
  1. urgency is High
  2. the customer is very angry (sentiment "Angry", our top band)
  3. the Knowledge Base Agent found no matching document
  4. the complaint involves a legal threat, fraud, a payment dispute or a product safety
     hazard (smoke, sparks, burns, injury)
  5. the reply's confidence is below the threshold (default 0.70)
  6. repeat contact: the same customer (email or order ID) raised a ticket in the same category
     in the last 14 days, so the earlier answer did not settle it
  7. the Log Agent's own judgement says a human should handle it (recorded as a reason only
     when no rule fired, so the reasons always show what actually caused the escalation)

Rules 1-6 are deterministic and cannot be overridden by the LLM. Rule 4 combines the
Query Agent's risk flags with a keyword safety net, so a missed flag still escalates.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from core.schemas import ConfidenceReport, EscalationDecision, TriageResult

RISK_PATTERNS: dict[str, re.Pattern[str]] = {
    "legal_threat": re.compile(
        r"\b(lawyer|advocate|attorney|legal (action|notice)|sue|suing|court|"
        r"consumer (forum|court|commission)|police complaint|file a (case|complaint against))\b",
        re.IGNORECASE,
    ),
    "fraud": re.compile(
        r"\b(fraud|fraudulent|scam|unauthori[sz]ed|stolen|hacked|"
        r"(did not|didn't|never) (place|make|authori[sz]e)|someone else (used|placed|ordered))\b",
        re.IGNORECASE,
    ),
    "safety_hazard": re.compile(
        r"\b(smok(e|ed|ing)|spark(s|ed|ing)?|burn(t|ed|s|ing)?|fire|caught fire|electric shock|"
        r"shocked|injur(y|ed|ies)|hurt|bleeding|melted|overheat(ed|ing)?)\b",
        re.IGNORECASE,
    ),
    "payment_dispute": re.compile(
        r"\b(charge ?back|dispute|contact(ing)? (my|the) bank|complain(ing)? to (my|the) bank|"
        r"bank complaint|report(ing)? (this|it) to (my|the) bank)\b",
        re.IGNORECASE,
    ),
}

RISK_LABELS = {
    "legal_threat": "Legal threat",
    "fraud": "Possible fraud",
    "payment_dispute": "Payment dispute",
    "safety_hazard": "Product safety risk",
}


REPEAT_WINDOW_DAYS = 14


def detect_risk_flags(text: str) -> set[str]:
    """Keyword safety net for the high-risk topics the escalation policy names explicitly."""
    return {flag for flag, pattern in RISK_PATTERNS.items() if pattern.search(text)}


def repeat_contact(prior_tickets: list[dict], category: str | None, now: datetime) -> str | None:
    """Reason text when this customer keeps coming back about the same kind of issue, else None."""
    if not category:
        return None
    since = now - timedelta(days=REPEAT_WINDOW_DAYS)
    same = [
        t for t in prior_tickets
        if t.get("category") == category and datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S") >= since
    ]
    if not same:
        return None
    still_open = [t for t in same if t.get("desk_status") == "Escalated"]
    if still_open:
        return f"Repeat contact: {still_open[0]['ticket_id']} about this is still open"
    # The customer came back about the same kind of issue, so the earlier answer did not settle it.
    earlier = ", ".join(t["ticket_id"] for t in same)
    return f"Repeat contact: earlier {category} ticket {earlier} within {REPEAT_WINDOW_DAYS} days"


def decide_escalation(
    triage: TriageResult | None,
    complaint: str,
    no_match: bool,
    confidence: ConfidenceReport | None,
    threshold: float,
    agent_escalate: bool = False,
    agent_reason: str = "",
    prior_tickets: list[dict] | None = None,
    now: datetime | None = None,
) -> EscalationDecision:
    reasons: list[str] = []

    # Small talk is only trusted when it looks harmless; anything urgent or angry is treated as support.
    small_talk = (
        triage is not None and triage.intent == "small_talk" and triage.urgency == "Low" and triage.sentiment != "Angry"
    )
    if triage is None:
        reasons.append("Triage unavailable")
    elif small_talk:
        pass  # greetings and acknowledgements: only the risk keywords below can escalate
    else:
        if triage.urgency == "High":
            reasons.append("High urgency")
        if triage.sentiment == "Angry":
            reasons.append("Very angry customer")

    if no_match and not small_talk:
        reasons.append("No matching knowledge-base document")

    llm_flags = set(triage.risk_flags) if triage else set()
    keyword_flags = detect_risk_flags(complaint)
    for flag in ("legal_threat", "fraud", "payment_dispute", "safety_hazard"):
        if flag in llm_flags or flag in keyword_flags:
            reasons.append(RISK_LABELS[flag])

    if small_talk:
        pass
    elif confidence is None:
        reasons.append("No resolution drafted")
    elif not no_match and confidence.score < threshold:
        reasons.append(f"Low confidence ({confidence.score:.2f} < {threshold:.2f})")

    repeat = None if small_talk else repeat_contact(prior_tickets or [], triage.category if triage else None, now or datetime.now())
    if repeat:
        reasons.append(repeat)

    # The Log Agent's judgement is a reason only when it changes the outcome (no rule fired).
    # Its opinion is always kept separately as the ticket's agent_recommendation.
    if agent_escalate and not reasons and not small_talk:
        detail = (agent_reason.strip() or "needs human review").replace(";", ",")  # "; " separates reasons
        reasons.append(f"Log Agent judgement: {detail}")

    return EscalationDecision(status="Escalated" if reasons else "Resolved", reasons=reasons)
