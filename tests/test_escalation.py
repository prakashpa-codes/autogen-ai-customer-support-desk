import pytest

from core.escalation import decide_escalation, detect_risk_flags
from core.schemas import ConfidenceReport, TriageResult

GOOD = ConfidenceReport(score=0.9, components={})
LOW = ConfidenceReport(score=0.55, components={})


def triage(**overrides):
    data = dict(
        intent="support", category="Shipping", urgency="Medium", sentiment="Neutral", summary="s", product=None,
        language="English", risk_flags=[], missing_details=[], search_query="q",
    )
    data.update(overrides)
    return TriageResult(**data)


def decide(t=None, complaint="Where is my order?", no_match=False, confidence=GOOD, **kw):
    return decide_escalation(t or triage(), complaint, no_match, confidence, 0.70, **kw)


def test_clean_ticket_is_resolved():
    d = decide()
    assert d.status == "Resolved" and d.reasons == []


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"t": triage(urgency="High")}, "High urgency"),
        ({"t": triage(sentiment="Angry")}, "Very angry customer"),
        ({"no_match": True}, "No matching knowledge-base document"),
        ({"t": triage(risk_flags=["fraud"])}, "Possible fraud"),
        ({"confidence": LOW}, "Low confidence (0.55 < 0.70)"),
        ({"agent_escalate": True, "agent_reason": "safety risk"}, "Log Agent judgement: safety risk"),
    ],
)
def test_each_rule_escalates_on_its_own(kwargs, reason):
    d = decide(**kwargs)
    assert d.status == "Escalated"
    assert reason in d.reasons


def test_keyword_safety_net_catches_missed_flags():
    d = decide(complaint="Fix this now or I'm contacting my bank and a lawyer.")
    assert {"Legal threat", "Payment dispute"} <= set(d.reasons)


def test_pdf_scenario_five_collects_all_reasons():
    t = triage(category="Billing", urgency="High", sentiment="Angry", risk_flags=["payment_dispute", "legal_threat"])
    d = decide(t, "You charged my card three times! Fix this now or I'm contacting my bank and a lawyer.")
    assert d.status == "Escalated"
    assert d.reasons[:2] == ["High urgency", "Very angry customer"]


def test_low_confidence_is_not_double_counted_when_nothing_matched():
    d = decide(no_match=True, confidence=ConfidenceReport(score=0.0, components={}))
    assert not any(r.startswith("Low confidence") for r in d.reasons)


def test_agent_judgement_is_a_reason_only_when_it_changes_the_outcome():
    d = decide(triage(urgency="High"), agent_escalate=True, agent_reason="customer is upset; wants a call")
    assert d.reasons == ["High urgency"]  # the rule already explains the escalation
    d = decide(agent_escalate=True, agent_reason="repeat contact; still unresolved")
    assert d.reasons == ["Log Agent judgement: repeat contact, still unresolved"]  # no '; ' inside a reason


def test_agent_cannot_un_escalate_a_rule():
    d = decide(triage(urgency="High"), agent_escalate=False)
    assert d.status == "Escalated"


@pytest.mark.parametrize(
    "text, flag",
    [
        ("I will take you to consumer court", "legal_threat"),
        ("Someone else used my card, this is fraud", "fraud"),
        ("I have raised a chargeback", "payment_dispute"),
        ("The blender started smoking and my hand got burnt", "safety_hazard"),
    ],
)
def test_detect_risk_flags(text, flag):
    assert flag in detect_risk_flags(text)


def test_safety_hazard_is_not_reported_as_fraud():
    d = decide(complaint="My PowerBlend started smoking and sparking and my hand got slightly burnt.")
    assert "Product safety risk" in d.reasons
    assert "Possible fraud" not in d.reasons


def test_ordinary_text_has_no_risk_flags():
    assert detect_risk_flags("My shoes are too tight, can I exchange them?") == set()


def test_repeat_contact_rule():
    from datetime import datetime

    now = datetime(2026, 11, 2, 12, 0, 0)
    open_ticket = [{"ticket_id": "TKT-0019", "created_at": "2026-11-01 10:00:00", "category": "Shipping",
                    "status": "Escalated", "desk_status": "Escalated"}]
    d = decide(prior_tickets=open_ticket, now=now)
    assert "Repeat contact: TKT-0019 about this is still open" in d.reasons

    # Coming back about the same kind of issue after an answer marked Resolved also counts.
    resolved_before = [{"ticket_id": "TKT-0010", "created_at": "2026-10-28 10:00:00", "category": "Shipping",
                        "status": "Resolved", "desk_status": "Resolved"}]
    assert "Repeat contact: earlier Shipping ticket TKT-0010 within 14 days" in decide(prior_tickets=resolved_before, now=now).reasons

    old_or_other = [
        {"ticket_id": "TKT-0001", "created_at": "2026-09-01 10:00:00", "category": "Shipping", "status": "Escalated", "desk_status": "Escalated"},
        {"ticket_id": "TKT-0002", "created_at": "2026-11-01 10:00:00", "category": "Billing", "status": "Escalated", "desk_status": "Escalated"},
    ]
    assert decide(prior_tickets=old_or_other, now=now).status == "Resolved"  # too old / different issue


def test_small_talk_is_not_escalated_unless_risky():
    chat = triage(intent="small_talk", category="Other", urgency="Low")
    d = decide(chat, complaint="who are you", no_match=True, confidence=None, agent_escalate=True, agent_reason="not documented")
    assert d.status == "Resolved"
    assert "Legal threat" in decide(chat, complaint="ok, my lawyer will call you", no_match=True, confidence=None).reasons
    # "Small talk" that is urgent or angry is treated as support.
    assert decide(triage(intent="small_talk", urgency="High"), no_match=True).status == "Escalated"
