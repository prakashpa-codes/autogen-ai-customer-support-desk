"""Tickets: the landing page. Every ticket with a colour-coded status; click any row to open and work it.

A ticket is Resolved (the AI answered it), Escalated (waiting for a person) or Closed (staff
handled it). Staff close an escalated ticket, with or without emailing a reply, and can reopen
any ticket.
"""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from desk.api import Backend, BackendError
from desk.ui import TimelineRenderer, badge, confidence_breakdown, reply_box

COLUMNS = [
    "ticket_id", "desk_status", "created_at", "customer_name", "channel", "category", "urgency",
    "sentiment", "confidence", "escalation_reason", "complaint",
]
STATUS_STYLE = {
    "Resolved": "background-color: rgba(12,163,12,.14); color: #006300; font-weight: 600",
    "Escalated": "background-color: rgba(208,59,59,.12); color: #a32424; font-weight: 600",
    "Closed": "background-color: #efeee9; color: #55534d; font-weight: 600",
}
EMAIL_CHANNELS = {"Email", "Web Form"}
CITATION_MARK_RE = re.compile(r" ?\[[SO]\d+\]")


def render(api: Backend) -> None:
    head, refresh = st.columns([6, 1], vertical_alignment="bottom")
    head.title("Tickets")
    # New email and chat tickets arrive in the background; any click reruns the page and reloads the list.
    refresh.button("Refresh", icon=":material/refresh:", use_container_width=True)
    if flash := st.session_state.pop("tickets_flash", None):
        st.toast(flash, icon=":material/check_circle:")

    f1, f2, f3, f4 = st.columns([1, 1, 1, 2])
    status = f1.selectbox("Status", ["", "Escalated", "Resolved", "Closed"])
    category = f2.selectbox("Category", ["", "Shipping", "Refund", "Account", "Product", "Billing", "Other"])
    channel = f3.selectbox("Channel", ["", "Chat", "Email", "Web Form"])
    search = f4.text_input("Search", placeholder="Ticket ID, name, order ID or text")

    try:
        tickets = [t for t in api.tickets(status=status, category=category, channel=channel, search=search) if t.get("desk_status")]
    except BackendError as exc:
        st.error(str(exc))
        return

    if not tickets:
        st.info("No tickets match these filters yet.")
        return

    df = pd.DataFrame(tickets).reindex(columns=COLUMNS)
    styled = (
        df.style.map(lambda v: STATUS_STYLE.get(v, ""), subset=["desk_status"])
        .format({"confidence": "{:.2f}"}, na_rep="")
    )
    selection = st.dataframe(
        styled,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode=["single-row", "single-cell"],  # clicking any cell opens the ticket
        # A fresh key after each open clears the selection, so filters or reruns never re-open it.
        key=f"tickets_table_{st.session_state.get('tickets_table_version', 0)}",
        column_config={
            "ticket_id": "Ticket",
            "desk_status": "Status",
            "created_at": st.column_config.DatetimeColumn("Received", format="DD MMM, HH:mm"),
            "customer_name": "Customer",
            "channel": "Channel",
            "category": "Category",
            "urgency": "Urgency",
            "sentiment": "Sentiment",
            "confidence": "Confidence",
            "escalation_reason": st.column_config.TextColumn("Escalation reason", width="medium"),
            "complaint": st.column_config.TextColumn("Message", width="large"),
        },
    )
    waiting = int((df["desk_status"] == "Escalated").sum())
    st.caption(f"{len(df)} tickets · {waiting} waiting for staff · click a ticket to open it")

    picked = None
    if selection and selection.selection:
        if selection.selection.rows:
            picked = selection.selection.rows[0]
        elif selection.selection.cells:
            picked = selection.selection.cells[0][0]
    if picked is not None:
        st.session_state["tickets_table_version"] = st.session_state.get("tickets_table_version", 0) + 1
        ticket_dialog(api, df.iloc[picked]["ticket_id"])

    with st.expander("Downloads"):
        d1, d2, d3 = st.columns(3)
        try:
            d1.download_button("Ticket log (CSV)", api.log_file(), "tickets.csv", "text/csv", icon=":material/download:")
        except BackendError:
            d1.button("Ticket log (CSV)", disabled=True, help="No tickets logged yet")
        d2.download_button("Export (CSV)", api.export("csv"), "tickets_export.csv", "text/csv", icon=":material/table:")
        d3.download_button(
            "Export (Excel)", api.export("xlsx"), "tickets.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/grid_on:",
        )


@st.dialog("Ticket details", width="large")
def ticket_dialog(api: Backend, ticket_id: str) -> None:
    t = api.ticket(ticket_id)
    st.markdown(f"### {ticket_id} &nbsp; {badge(t['desk_status'])}", unsafe_allow_html=True)
    st.markdown(f"**{t.get('customer_name') or 'Customer'}** · {t['channel']} · {t['created_at']}"
                + (f" · {t['customer_email']}" if t.get("customer_email") else "")
                + (f" · {t['order_id']}" if t.get("order_id") else ""))
    st.markdown(f"> {t['complaint']}")
    if t.get("status") == "Escalated" and t.get("escalation_reason"):
        st.markdown(f"**Why escalated:** {t['escalation_reason']}")
    st.markdown("**AI reply sent to the customer**")
    st.markdown(reply_box(t.get("response") or "", t["status"]), unsafe_allow_html=True)
    if t.get("final_reply"):
        st.markdown("**Staff reply**")
        st.markdown(reply_box(t["final_reply"], "Resolved"), unsafe_allow_html=True)
    if t.get("reviewed_by"):
        st.caption(f"{t.get('review_status')} by {t['reviewed_by']} at {t['reviewed_at']}"
                   + (f" · note: {t['review_note']}" if t.get("review_note") else ""))

    with st.container(border=True):
        if t["desk_status"] == "Escalated":
            close_actions(api, t)
        else:
            reopen_action(api, t)

    with st.expander("Confidence breakdown"):
        confidence_breakdown(t.get("confidence_detail"))
    with st.expander("Agent timeline"):
        renderer = TimelineRenderer(live=False)
        for event in api.timeline(ticket_id):
            renderer.handle(event)


def _done(message: str) -> None:
    """Close the pop-up and refresh the table; the message shows as a toast."""
    st.session_state["tickets_flash"] = message
    st.rerun()


def close_actions(api: Backend, t: dict) -> None:
    ticket_id = t["ticket_id"]
    can_email = t["channel"] in EMAIL_CHANNELS and bool(t.get("customer_email"))
    st.markdown("**Handle this ticket**")
    note = st.text_input("Internal note (optional)", key=f"note_{ticket_id}")

    if not can_email:
        st.caption("The customer can't be reached from here (chat, or no email address), so the ticket is closed with a note.")
        if st.button("Close ticket", type="primary", icon=":material/task_alt:", key=f"close_{ticket_id}"):
            _act(lambda: api.close(ticket_id, note=note), f"{ticket_id} closed.")
        return

    draft_key = f"draft_{ticket_id}"
    if draft_key not in st.session_state:
        # Start from the agent's answer without the "passed to our senior team" notice (it only belongs
        # in the first reply) and without the [S1] citation marks.
        answer = CITATION_MARK_RE.sub("", t.get("response_annotated") or "").strip()
        st.session_state[draft_key] = answer or t.get("response") or ""
    # A revised draft arrives on the run after "Ask AI to revise"; a widget's value can only be set
    # before the widget is drawn, so it is staged under a separate key and applied here.
    if (staged := st.session_state.pop(f"staged_{ticket_id}", None)) is not None:
        st.session_state[draft_key] = staged

    reply = st.text_area("Reply to the customer", key=draft_key, height=200)
    with st.expander("Ask AI to revise the reply"):
        guidance = st.text_input(
            "Guidance for the Resolution Agent", key=f"guidance_{ticket_id}",
            placeholder="e.g. Apologise for the delay and offer a free exchange for the bigger size.",
        )
        if st.button("Revise with AI", icon=":material/auto_fix_high:", key=f"revise_{ticket_id}", disabled=len(guidance) < 3):
            with st.spinner("The Resolution Agent is redrafting from the evidence…"):
                try:
                    result = api.revise(ticket_id, guidance)
                except BackendError as exc:
                    st.error(str(exc))
                else:
                    st.session_state[f"staged_{ticket_id}"] = result["customer_draft"]
                    st.session_state[f"revised_conf_{ticket_id}"] = result["confidence"]
                    st.rerun(scope="fragment")  # rerun only the pop-up, so it stays open
        if (conf := st.session_state.get(f"revised_conf_{ticket_id}")) is not None:
            st.success(f"Draft revised by the Resolution Agent · confidence {conf:.2f}. Check it above before sending.")

    c1, c2 = st.columns(2)
    if c1.button("Send reply & close", type="primary", icon=":material/send:", key=f"send_{ticket_id}",
                 disabled=len(reply.strip()) < 5, use_container_width=True):
        _act(lambda: api.close(ticket_id, reply=reply, note=note), f"{ticket_id}: reply emailed and ticket closed.")
    if c2.button("Close without reply", icon=":material/task_alt:", key=f"close_{ticket_id}", use_container_width=True):
        _act(lambda: api.close(ticket_id, note=note), f"{ticket_id} closed without a reply.")


def reopen_action(api: Backend, t: dict) -> None:
    ticket_id = t["ticket_id"]
    note = st.text_input("Reason for reopening (optional)", key=f"reopen_note_{ticket_id}")
    if st.button("Reopen ticket", icon=":material/restart_alt:", key=f"reopen_{ticket_id}"):
        _act(lambda: api.reopen(ticket_id, note=note), f"{ticket_id} reopened: it is waiting for staff again.")


def _act(call, message: str) -> None:
    try:
        result = call()
    except BackendError as exc:
        st.error(str(exc))
        return
    if result.get("email_delivery") == "failed":
        message += " The email could not be sent; see the Email page."
    for key in [k for k in st.session_state if k.endswith(f"_{result['ticket_id']}")]:
        del st.session_state[key]
    _done(message)
