"""New Ticket: submit a complaint and watch the four agents work on it live."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from desk.api import Backend, BackendError
from desk.ui import TimelineRenderer

SCENARIOS = json.loads((Path(__file__).resolve().parents[2] / "evaluation" / "scenarios.json").read_text(encoding="utf-8"))


def _load_sample() -> None:
    choice = st.session_state.get("sample")
    sample = next((s for s in SCENARIOS if f"{s['id']} · {s['complaint'][:60]}" == choice), None)
    if sample:
        st.session_state.update(
            nt_complaint=sample["complaint"],
            nt_name=sample.get("customer_name", ""),
            nt_order=sample.get("order_id", ""),
            nt_channel=sample.get("channel", "Chat"),
            nt_subject=sample.get("subject", ""),
            nt_email=f"{sample.get('customer_name', 'customer').split()[0].lower()}@example.com"
            if sample.get("channel") == "Email" else "",
        )


def render(api: Backend) -> None:
    st.title("New ticket")
    st.caption("Submit a customer complaint. The Query, Knowledge, Resolution and Log agents handle it in order.")

    st.selectbox(
        "Load a sample complaint",
        [""] + [f"{s['id']} · {s['complaint'][:60]}" for s in SCENARIOS],
        key="sample",
        on_change=_load_sample,
        help="Reference scenarios plus additional test cases, including Tamil and Bengali messages.",
    )

    with st.form("new_ticket"):
        c1, c2, c3 = st.columns([1.2, 1, 1])
        channel = c1.segmented_control("Channel", ["Chat", "Email", "Web Form"], key="nt_channel", default="Chat")
        name = c2.text_input("Customer name (optional)", key="nt_name")
        order_id = c3.text_input("Order ID (optional)", key="nt_order", placeholder="ORD-48213")
        e1, e2 = st.columns(2)
        email = e1.text_input("Customer email (email channels)", key="nt_email")
        subject = e2.text_input("Email subject (optional)", key="nt_subject")
        complaint = st.text_area("Complaint", key="nt_complaint", height=130, placeholder="Describe the customer's issue…")
        submitted = st.form_submit_button("Submit ticket", type="primary", icon=":material/send:")

    if submitted:
        if len((complaint or "").strip()) < 5:
            st.warning("Please enter the complaint text.")
            return
        try:
            created = api.create_ticket(
                complaint=complaint, channel=channel or "Chat", customer_name=name, order_id=order_id,
                customer_email=email, subject=subject,
            )
        except BackendError as exc:
            st.error(str(exc))
            return
        st.session_state["last_ticket"] = created["ticket_id"]
        st.subheader(f"{created['ticket_id']} · live agent timeline")
        renderer = TimelineRenderer(live=True)
        try:
            for event in api.follow(created["ticket_id"]):
                renderer.handle(event)
        except BackendError as exc:
            st.error(str(exc))
    elif st.session_state.get("last_ticket"):
        ticket_id = st.session_state["last_ticket"]
        st.subheader(f"{ticket_id} · agent timeline")
        renderer = TimelineRenderer(live=False)
        try:
            for event in api.timeline(ticket_id):
                renderer.handle(event)
        except BackendError as exc:
            st.error(str(exc))
