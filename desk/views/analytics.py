"""Analytics: headline KPIs and charts for tickets, escalations and confidence."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from desk.api import Backend, BackendError

BLUE = "#2a78d6"
URGENCY_RAMP = ["#86b6ef", "#2a78d6", "#104281"]  # ordinal Low -> High, one hue
STATUS_COLORS = {"Resolved": "#0ca30c", "Escalated": "#d03b3b"}
THRESHOLD = 0.70


def _bar(df: pd.DataFrame, x: str, y: str, title: str, color: str = BLUE, sort: str = "-x") -> alt.Chart:
    return (
        alt.Chart(df, title=title)
        .mark_bar(color=color, cornerRadiusEnd=4, size=18)
        .encode(
            x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(tickMinStep=1, grid=True, gridOpacity=0.4)),
            y=alt.Y(f"{y}:N", title=None, sort=sort, axis=alt.Axis(labelLimit=260)),
            tooltip=[alt.Tooltip(f"{y}:N"), alt.Tooltip(f"{x}:Q")],
        )
        .properties(height=alt.Step(28))
    )


def render(api: Backend) -> None:
    st.title("Analytics")
    try:
        s = api.analytics()
    except BackendError as exc:
        st.error(str(exc))
        return
    if not s.get("total"):
        st.info("No processed tickets yet. Submit a few tickets to see analytics.")
        return

    k = st.columns(6)
    k[0].metric("Tickets", s["total"])
    k[1].metric("Resolved by agents", f"{s['resolution_rate']:.0%}")
    k[2].metric("Escalated", s["escalated"])
    k[3].metric("Waiting for staff", s["waiting"])
    k[4].metric("Avg confidence", f"{s['avg_confidence']:.2f}")
    k[5].metric("Avg urgency", f"{s['avg_urgency']:.2f}", help="Low = 1, Medium = 2, High = 3")
    st.caption(
        f"Average processing time {s['avg_duration_s'] or '-'} s · {s['total_tokens']:,} tokens used in total"
    )

    left, right = st.columns(2)

    # Tickets per category, split by outcome (status colours carry a legend + tooltip, never colour alone).
    cat = pd.DataFrame(s["by_category"]).melt(id_vars="category", var_name="status", value_name="tickets")
    cat = cat[cat["status"].isin(STATUS_COLORS)]
    with left:
        chart = (
            alt.Chart(cat, title="Tickets per category")
            .mark_bar(size=18, stroke="white", strokeWidth=2)
            .encode(
                x=alt.X("sum(tickets):Q", title=None, axis=alt.Axis(tickMinStep=1, gridOpacity=0.4)),
                y=alt.Y("category:N", title=None, sort="-x"),
                color=alt.Color("status:N", scale=alt.Scale(domain=list(STATUS_COLORS), range=list(STATUS_COLORS.values())),
                                legend=alt.Legend(orient="top", title=None)),
                order=alt.Order("status:N", sort="descending"),
                tooltip=["category:N", "status:N", "tickets:Q"],
            )
            .properties(height=alt.Step(28))
        )
        st.altair_chart(chart, use_container_width=True)

    with right:
        reasons = pd.DataFrame(list(s["escalation_reasons"].items()), columns=["reason", "tickets"])
        if reasons.empty:
            st.caption("No escalations yet.")
        else:
            st.altair_chart(_bar(reasons, "tickets", "reason", "Why tickets were escalated"), use_container_width=True)

    left, right = st.columns(2)
    with left:
        urgency = pd.DataFrame(list(s["by_urgency"].items()), columns=["urgency", "tickets"])
        chart = (
            alt.Chart(urgency, title="Urgency mix")
            .mark_bar(cornerRadiusEnd=4, size=36)
            .encode(
                x=alt.X("urgency:N", title=None, sort=["Low", "Medium", "High"], axis=alt.Axis(labelAngle=0)),
                y=alt.Y("tickets:Q", title=None, axis=alt.Axis(tickMinStep=1, gridOpacity=0.4)),
                color=alt.Color("urgency:N", scale=alt.Scale(domain=["Low", "Medium", "High"], range=URGENCY_RAMP), legend=None),
                tooltip=["urgency:N", "tickets:Q"],
            )
        )
        st.altair_chart(chart, use_container_width=True)
    with right:
        conf = pd.DataFrame(list(s["confidence_by_category"].items()), columns=["category", "confidence"])
        bars = (
            alt.Chart(conf, title="Average confidence by category")
            .mark_bar(color=BLUE, cornerRadiusEnd=4, size=18)
            .encode(
                x=alt.X("confidence:Q", title=None, scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(gridOpacity=0.4)),
                y=alt.Y("category:N", title=None, sort="-x"),
                tooltip=["category:N", alt.Tooltip("confidence:Q", format=".2f")],
            )
            .properties(height=alt.Step(28))
        )
        rule = alt.Chart(pd.DataFrame({"x": [THRESHOLD]})).mark_rule(color="#52514e", strokeDash=[4, 3]).encode(x="x:Q")
        st.altair_chart(bars + rule, use_container_width=True)
        st.caption(f"Dashed line: escalation threshold {THRESHOLD:.2f}")

    left, right = st.columns(2)
    with left:
        channels = pd.DataFrame(list(s["by_channel"].items()), columns=["channel", "tickets"])
        st.altair_chart(_bar(channels, "tickets", "channel", "Tickets by channel"), use_container_width=True)
    with right:
        languages = pd.DataFrame(list(s["by_language"].items()), columns=["language", "tickets"])
        st.altair_chart(_bar(languages, "tickets", "language", "Customer languages"), use_container_width=True)

    daily = pd.DataFrame(list(s["daily_volume"].items()), columns=["date", "tickets"])
    if len(daily) > 1:
        line = (
            alt.Chart(daily, title="Daily ticket volume")
            .mark_line(color=BLUE, strokeWidth=2, point=alt.OverlayMarkDef(size=64, color=BLUE))
            .encode(x=alt.X("date:T", title=None), y=alt.Y("tickets:Q", title=None, axis=alt.Axis(tickMinStep=1)),
                    tooltip=["date:T", "tickets:Q"])
        )
        st.altair_chart(line, use_container_width=True)

    with st.expander("Table view"):
        st.dataframe(cat.pivot_table(index="category", columns="status", values="tickets", fill_value=0), use_container_width=True)
