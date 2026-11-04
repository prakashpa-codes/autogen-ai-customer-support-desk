"""Shared dashboard components: styling, status badges, and the live agent timeline."""

from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

AGENTS: dict[str, tuple[str, str, str]] = {
    "query_agent": ("Customer Query Agent", "Classifies the complaint", ":material/psychology:"),
    "knowledge_agent": ("Knowledge Base Agent", "Finds the right policy", ":material/menu_book:"),
    "resolution_agent": ("Resolution Agent", "Writes a grounded reply", ":material/edit_note:"),
    "log_agent": ("Log Agent", "Logs and decides escalation", ":material/inventory_2:"),
}
AGENT_ORDER = list(AGENTS)

CSS = """
<style>
:root { --navy:#0b2545; --gold:#e0a526; --good:#0ca30c; --good-ink:#006300; --critical:#d03b3b; --muted:#6b6a65; }
.block-container { padding-top: 2.2rem; }
.rd-brand { display:flex; align-items:center; gap:.6rem; margin-bottom:.2rem; }
.rd-brand .mark { background:var(--navy); color:var(--gold); font-weight:800; border-radius:8px; padding:.15rem .5rem; }
.rd-pipeline { display:flex; flex-wrap:wrap; gap:.4rem; align-items:center; margin:.4rem 0 1rem; }
.rd-step { border:1px solid rgba(11,37,69,.18); border-radius:999px; padding:.3rem .8rem; font-size:.85rem; color:var(--muted); background:#fff; }
.rd-step.active { border-color:var(--gold); color:var(--navy); font-weight:600; box-shadow:0 0 0 3px rgba(224,165,38,.18); }
.rd-step.done { background:var(--navy); color:#fff; border-color:var(--navy); }
.rd-arrow { color:#b5b3ab; }
.rd-badge { display:inline-flex; align-items:center; gap:.35rem; border-radius:999px; padding:.25rem .75rem; font-weight:700; font-size:.85rem; }
.rd-badge.resolved { background:rgba(12,163,12,.12); color:var(--good-ink); border:1px solid rgba(12,163,12,.45); }
.rd-badge.escalated { background:rgba(208,59,59,.10); color:#a32424; border:1px solid rgba(208,59,59,.5); }
.rd-badge.neutral { background:#f1f0ec; color:#3d3c39; border:1px solid #e1e0d9; }
.rd-reply { border-left:4px solid var(--gold); background:#fffaf0; border-radius:8px; padding:1rem 1.1rem; line-height:1.55; color:#1b1b1a; }
.rd-reply.escalated { border-left-color:var(--critical); background:#fff6f6; }
.rd-chip { display:inline-block; background:#f1f0ec; border-radius:6px; padding:.1rem .45rem; margin:.1rem .2rem .1rem 0; font-size:.8rem; color:#3d3c39; }
.rd-chip.risk { background:rgba(208,59,59,.1); color:#a32424; }
.rd-source { font-size:.85rem; color:var(--muted); }
/* Rows in the ticket table open a ticket on click. The grid's scroll layer sits on top of its canvas,
   so that layer is what the mouse is over. */
[class*="st-key-tickets_table"] .dvn-scroller { cursor: pointer !important; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def badge(status: str | None) -> str:
    if status == "Resolved":
        return '<span class="rd-badge resolved">✔ Resolved</span>'
    if status == "Escalated":
        return '<span class="rd-badge escalated">▲ Escalated</span>'
    return f'<span class="rd-badge neutral">{html.escape(status or "Processing")}</span>'


def reply_box(text: str, status: str | None) -> str:
    css = "rd-reply escalated" if status == "Escalated" else "rd-reply"
    return f'<div class="{css}">{html.escape(text).replace(chr(10), "<br>")}</div>'


def chips(values: list[str], risk: bool = False) -> str:
    cls = "rd-chip risk" if risk else "rd-chip"
    return "".join(f'<span class="{cls}">{html.escape(v)}</span>' for v in values)


def confidence_breakdown(confidence: dict | None, threshold: float = 0.70) -> None:
    if not confidence:
        st.caption("No confidence score (no reply was submitted).")
        return
    score = confidence["score"]
    st.metric("Confidence", f"{score:.2f}", delta=f"{score - threshold:+.2f} vs {threshold:.2f} threshold",
              delta_color="normal")
    labels = {
        "evidence_strength": "Evidence strength (35%)",
        "citation_coverage": "Citation coverage (25%)",
        "fact_check": "Fact check (25%)",
        "category_alignment": "Category alignment (15%)",
    }
    for key, label in labels.items():
        value = confidence["components"].get(key, 0.0)
        st.progress(min(max(value, 0.0), 1.0), text=f"{label}: {value:.2f}")
    for note in confidence.get("notes", []):
        st.caption(f"• {note}")


def render_result(data: dict[str, Any]) -> None:
    """Final outcome card: badge, customer reply, reasons and sources."""
    status = data.get("status")
    st.markdown(f"#### Customer reply &nbsp; {badge(status)}", unsafe_allow_html=True)
    st.markdown(reply_box(data.get("customer_reply") or data.get("reply") or "", status), unsafe_allow_html=True)
    cols = st.columns(3)
    reasons = data.get("reasons") or []
    cols[0].markdown("**Escalation reasons**<br>" + (chips(reasons, risk=True) if reasons else "None"), unsafe_allow_html=True)
    sources = data.get("kb_sources") or []
    cols[1].markdown("**Knowledge sources**<br>" + (chips(sources) if sources else "None"), unsafe_allow_html=True)
    if data.get("confidence") is not None:
        cols[2].markdown(f"**Confidence**<br>{float(data['confidence']):.2f}", unsafe_allow_html=True)


class TimelineRenderer:
    """Renders agent events as a step-by-step timeline. Works live (SSE) and for replays."""

    def __init__(self, live: bool = True) -> None:
        self.live = live
        self.state = {name: "pending" for name in AGENT_ORDER}
        self.header = st.empty()
        self.body = st.container()
        self.result = st.container()
        self.current: dict[str, Any] = {}
        self.passes: dict[str, int] = {}
        self._draw_header()

    def _draw_header(self) -> None:
        parts = []
        for i, name in enumerate(AGENT_ORDER):
            label = AGENTS[name][0]
            parts.append(f'<span class="rd-step {self.state[name]}">{i + 1}. {label}</span>')
        self.header.markdown(
            '<div class="rd-pipeline">' + '<span class="rd-arrow">→</span>'.join(parts) + "</div>", unsafe_allow_html=True
        )

    def _container(self, agent: str):
        if agent not in self.current:
            self.passes[agent] = self.passes.get(agent, 0) + 1
            title, purpose, icon = AGENTS.get(agent, (agent, "", ":material/smart_toy:"))
            suffix = f" · pass {self.passes[agent]}" if self.passes[agent] > 1 else ""
            with self.body:
                self.current[agent] = st.status(f"{title}{suffix} — {purpose}", expanded=self.live, state="running")
        return self.current[agent]

    def handle(self, event: dict[str, Any]) -> None:
        kind, agent, data = event["type"], event.get("agent"), event.get("data") or {}
        if kind == "agent_started" and agent in AGENTS:
            self.state[agent] = "active"
            self._draw_header()
            self._container(agent)
        elif kind == "tool_call" and agent in AGENTS:
            args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in data.get("arguments", {}).items())
            if data.get("tool") in {"submit_resolution"}:
                args = "reply=…"
            self._container(agent).markdown(f":material/build: Tool call `{data.get('tool')}({args})`")
        elif kind == "tool_result" and agent in AGENTS and data.get("tool") == "search_kb":
            with self._container(agent).expander("Search results returned to the agent", expanded=False):
                st.text(data.get("content", ""))
        elif kind == "agent_message" and agent in AGENTS:
            with self._container(agent).expander("Message handed to the next agent", expanded=False):
                st.text(data.get("text", ""))
        elif kind == "agent_completed" and agent in AGENTS:
            self._render_snapshot(agent, data)
        elif kind == "routing":
            with self.body:
                st.caption(f":material/alt_route: Orchestrator: {data.get('text', '')}")
        elif kind == "ticket_completed":
            for name in AGENT_ORDER:
                if self.state[name] != "done" and name in self.passes:
                    self.state[name] = "done"
            self._draw_header()
            with self.result:
                render_result(data)
        elif kind == "ticket_failed":
            with self.result:
                st.error(f"Processing failed ({data.get('error')}). The ticket was logged and escalated.")
                if data.get("customer_reply"):
                    render_result({"status": "Escalated", "customer_reply": data["customer_reply"]})

    def _render_snapshot(self, agent: str, data: dict[str, Any]) -> None:
        box = self._container(agent)
        summary = ""
        with box:
            if agent == "query_agent" and data.get("triage"):
                t = data["triage"]
                c = st.columns(4)
                c[0].metric("Category", t["category"])
                c[1].metric("Urgency", t["urgency"])
                c[2].metric("Sentiment", t["sentiment"])
                c[3].metric("Language", t["language"])
                st.markdown(f"**Summary:** {t['summary']}")
                extras = [f"Product: {t['product']}"] if t.get("product") else []
                st.markdown(chips(extras + [f"Search: {t['search_query']}"]) + chips(t.get("risk_flags", []), risk=True),
                            unsafe_allow_html=True)
                if t.get("missing_details"):
                    st.markdown("**Needs from the customer:** " + chips(t["missing_details"]), unsafe_allow_html=True)
                summary = f"{t['category']} · {t['urgency']} · {t['sentiment']}"
            elif agent == "knowledge_agent":
                if data.get("no_match"):
                    st.warning("No matching document. The Resolution Agent must answer honestly and the ticket is escalated.")
                    summary = "no matching document"
                for p in data.get("evidence", []):
                    st.markdown(
                        f"**[{p['id']}] {p['source']}** › {p['section']} "
                        f"<span class='rd-source'>(similarity {p['similarity']:.2f})</span>",
                        unsafe_allow_html=True,
                    )
                    with st.expander("Passage text", expanded=False):
                        st.markdown(p["text"])
                if data.get("rationale"):
                    st.caption(f"Rationale: {data['rationale']}")
                lookup_notes = {
                    "not_verified": "Order lookup: order found, details withheld (the ticket's email does not match the order).",
                    "not_found": "Order lookup: no order with this ID.",
                }
                if data.get("order_lookup") in lookup_notes:
                    st.caption(f":material/lock: {lookup_notes[data['order_lookup']]}")
                prior = data.get("prior_tickets") or []
                if prior:
                    shared = data.get("identity_verified")
                    note = "shared with the agents" if shared else "not shared with the agents: identity not verified on this channel"
                    with st.expander(f"Customer history: {len(prior)} earlier ticket(s) ({note})", expanded=False):
                        for p in prior:
                            st.markdown(
                                f"**{p['ticket_id']}** · {p['created_at'][:16]} · {p.get('category') or '-'} · "
                                f"{p.get('desk_status') or p.get('status') or '-'}  \n"
                                f"{p.get('summary') or ''}"
                            )
                if not summary:
                    sources = sorted({p["source"] for p in data.get("evidence", [])})
                    summary = f"{len(data.get('evidence', []))} passages · {', '.join(sources)}"
            elif agent == "resolution_agent":
                confidence_breakdown(data.get("confidence"))
                with st.expander("Draft with citations", expanded=False):
                    st.text(data.get("reply", ""))
                if data.get("confidence"):
                    summary = f"confidence {data['confidence']['score']:.2f}"
            elif agent == "log_agent" and data.get("status"):
                st.markdown(badge(data["status"]), unsafe_allow_html=True)
                if data.get("reasons"):
                    st.markdown(chips(data["reasons"], risk=True), unsafe_allow_html=True)
                st.caption(f"Log Agent judgement: {data.get('agent_recommendation') or '-'}")
                summary = data["status"]
        title, purpose, _ = AGENTS[agent]
        suffix = f" · pass {self.passes[agent]}" if self.passes.get(agent, 1) > 1 else ""
        box.update(label=f"{title}{suffix} — {summary or purpose}", state="complete", expanded=False)
        self.state[agent] = "done"
        self.current.pop(agent, None)
        self._draw_header()
