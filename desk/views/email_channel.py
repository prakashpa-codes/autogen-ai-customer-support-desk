"""Email channel: Gmail status, the Inbox Agent's decisions, the inbound simulator and the outbox."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from desk.api import Backend, BackendError

SAMPLE_BODY = (
    "Hi team,\n\nI ordered Eden Rubber Stud Cricket Shoes in UK 8 (order ORD-51007) but they are too tight. "
    "Can I swap them for UK 9?\n\nThanks,\nRohit"
)


def render(api: Backend) -> None:
    st.title("Email channel")
    try:
        box = api.outbox()
    except BackendError as exc:
        st.error(str(exc))
        return

    gmail = box["gmail"]
    if gmail["connected"]:
        st.success("Gmail connected through the Gmail MCP server. The Inbox Agent checks the mailbox every minute; replies are sent from Gmail.", icon=":material/mark_email_read:")
        if gmail["last_error"]:
            st.warning(f"Last mailbox check failed: {gmail['last_error']}")
        if gmail["activity"]:
            st.subheader("Inbox Agent decisions")
            st.dataframe(
                pd.DataFrame(gmail["activity"]),
                hide_index=True,
                use_container_width=True,
                column_config={"at": "Checked at", "from": "From", "subject": "Subject", "decision": "Decision", "reason": "Reason"},
            )
    else:
        st.info("Gmail is not connected: replies are kept in the outbox. Set GMAIL_ENABLED=true after running scripts/gmail_auth.py.", icon=":material/mail:")

    with st.form("inbound"):
        st.subheader("Receive an email")
        st.caption("The pasted email becomes an Email-channel ticket; the agent reply lands in the outbox below.")
        sender = st.text_input("From", value="Rohit Das <rohit.das@example.com>")
        subject = st.text_input("Subject", value="Exchange shoes for a bigger size")
        body = st.text_area("Body", value=SAMPLE_BODY, height=150)
        if st.form_submit_button("Receive email", type="primary", icon=":material/mail:"):
            try:
                created = api.inbound_email(sender, subject, body)
            except BackendError as exc:
                st.error(str(exc))
            else:
                st.success(f"Created {created['ticket_id']}. Open the Tickets page to follow it; the reply will appear in the outbox.")

    st.subheader("Outbox")
    if not box["messages"]:
        st.caption("No emails sent yet.")
        return
    df = pd.DataFrame(box["messages"])[["created_at", "ticket_id", "to_addr", "subject", "delivery", "body"]]
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "created_at": "Sent at",
            "ticket_id": "Ticket",
            "to_addr": "To",
            "subject": "Subject",
            "delivery": st.column_config.TextColumn("Delivery", help="sent = delivered through Gmail; stored = kept in the outbox; failed = Gmail error (see the backend log)"),
            "body": st.column_config.TextColumn("Body", width="large"),
        },
    )
