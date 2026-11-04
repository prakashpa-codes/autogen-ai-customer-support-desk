"""Dashboard metrics computed from processed tickets."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pandas as pd

URGENCY_SCORE = {"Low": 1, "Medium": 2, "High": 3}


def normalise_reason(reason: str) -> str:
    """Group parameterised reasons, e.g. 'Low confidence (0.55 < 0.70)' -> 'Low confidence'."""
    reason = re.sub(r"\s*\(.*\)$", "", reason.strip())
    return reason.split(":")[0].strip()


def summarise(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    done = [t for t in tickets if t.get("status")]
    if not done:
        return {"total": 0}
    df = pd.DataFrame(done)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["urgency_score"] = df["urgency"].map(URGENCY_SCORE)

    reasons = Counter(
        normalise_reason(r)
        for value in df.loc[df["status"] == "Escalated", "escalation_reason"].fillna("")
        for r in value.split("; ")
        if r
    )
    by_category = (
        df.pivot_table(index="category", columns="status", values="ticket_id", aggfunc="count", fill_value=0)
        .reset_index()
        .to_dict("records")
    )
    daily = df.groupby(df["created_at"].dt.date).size()

    return {
        "total": int(len(df)),
        "resolved": int((df["status"] == "Resolved").sum()),
        "escalated": int((df["status"] == "Escalated").sum()),
        "waiting": int((df["desk_status"] == "Escalated").sum()),  # escalated and not yet closed
        "resolution_rate": round(float((df["status"] == "Resolved").mean()), 3),
        "avg_urgency": round(float(df["urgency_score"].mean()), 2),
        "avg_confidence": round(float(df["confidence"].fillna(0).mean()), 3),
        "avg_duration_s": round(float(df["duration_ms"].dropna().mean() / 1000), 1) if df["duration_ms"].notna().any() else None,
        "total_tokens": int(df[["prompt_tokens", "completion_tokens"]].fillna(0).sum().sum()),
        "by_category": by_category,
        "by_channel": df["channel"].value_counts().to_dict(),
        "by_urgency": df["urgency"].value_counts().reindex(["Low", "Medium", "High"], fill_value=0).to_dict(),
        "by_sentiment": df["sentiment"].value_counts().to_dict(),
        "by_language": df["language"].fillna("Unknown").value_counts().to_dict(),
        "escalation_reasons": dict(reasons.most_common()),
        "confidence_by_category": df.groupby("category")["confidence"].mean().round(3).to_dict(),
        "daily_volume": {str(day): int(n) for day, n in daily.items()},
    }
