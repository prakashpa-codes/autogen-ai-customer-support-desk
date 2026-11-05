"""Ticket persistence.

Two stores with different jobs:
  * SQLite (data/resolvedesk.db) is the operational database: ticket state, agent event
    timeline, human reviews and the email outbox. It supports updates and queries.
  * logs/tickets.csv is the ticket log: one row per ticket with the required columns, written
    by the Log Agent's log_ticket tool. A chat conversation is one ticket, so when a later
    message is processed its row is replaced with the latest result. Staff actions are
    appended to logs/reviews.csv, which is the audit trail and is never rewritten.

The backend is the only process that writes here, which is why plain SQLite is enough.
"""

from __future__ import annotations

import csv
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.privacy import mask_sensitive

if TYPE_CHECKING:
    from core.schemas import TicketContext

LOG_COLUMNS = [
    "ticket_id",
    "timestamp",
    "customer_name",
    "order_id",
    "channel",
    "complaint",
    "category",
    "urgency",
    "sentiment",
    "product",
    "language",
    "kb_sources",
    "response",
    "confidence",
    "status",
    "escalation_reason",
]

REVIEW_COLUMNS = ["ticket_id", "reviewed_at", "reviewer", "action", "note", "final_reply"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT UNIQUE,
    access_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL,              -- queued | processing | completed | failed
    channel TEXT NOT NULL,
    customer_name TEXT, customer_email TEXT, order_id TEXT, subject TEXT,
    complaint TEXT NOT NULL,
    identity_verified INTEGER NOT NULL DEFAULT 0,
    raised_by_staff INTEGER NOT NULL DEFAULT 0,
    category TEXT, urgency TEXT, sentiment TEXT, product TEXT, language TEXT, summary TEXT, risk_flags TEXT,
    missing_details TEXT, conversation TEXT,
    kb_sources TEXT, passages_json TEXT, evidence_json TEXT,
    response TEXT, response_annotated TEXT,
    confidence REAL, confidence_json TEXT,
    status TEXT, escalation_reason TEXT, agent_recommendation TEXT,
    review_status TEXT, final_reply TEXT, reviewed_by TEXT, reviewed_at TEXT, review_note TEXT,
    team_mode TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, duration_ms INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    ticket_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    agent TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (ticket_id, seq)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivery TEXT NOT NULL,           -- sent | stored | failed
    error TEXT
);
"""


# Columns added after the first release: created on older databases by _migrate().
_ADDED_COLUMNS = {
    "identity_verified": "INTEGER NOT NULL DEFAULT 0",
    "raised_by_staff": "INTEGER NOT NULL DEFAULT 0",
    "missing_details": "TEXT",
    "conversation": "TEXT",
    "email_thread": "TEXT",  # JSON {thread_id, message_id_header} of the Gmail message, to reply in-thread
    # Where the ticket stands for staff: Resolved (AI answered), Escalated (waiting for a person) or Closed.
    # `status` keeps the AI decision (Resolved / Escalated) that the ticket log records.
    "desk_status": "TEXT",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TicketStore:
    def __init__(self, db_path: Path, log_dir: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = log_dir / "tickets.csv"
        self.reviews_csv_path = log_dir / "reviews.csv"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(tickets)")}
        for column, definition in _ADDED_COLUMNS.items():
            if column not in existing:
                self._db.execute(f"ALTER TABLE tickets ADD COLUMN {column} {definition}")
        # Tickets from before desk_status: approved reviews count as closed.
        self._db.execute(
            "UPDATE tickets SET desk_status = CASE WHEN review_status IN ('Approved', 'Revised & approved')"
            " THEN 'Closed' ELSE status END WHERE desk_status IS NULL AND status IS NOT NULL"
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ tickets

    def create_ticket(
        self,
        complaint: str,
        channel: str,
        customer_name: str = "",
        order_id: str = "",
        customer_email: str = "",
        subject: str = "",
        identity_verified: bool = False,
        raised_by_staff: bool = False,
    ) -> dict[str, Any]:
        complaint, subject = mask_sensitive(complaint), mask_sensitive(subject)
        token = secrets.token_urlsafe(16)
        now = _now()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO tickets (access_token, created_at, updated_at, state, channel, customer_name,"
                " customer_email, order_id, subject, complaint, identity_verified, raised_by_staff, conversation)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (token, now, now, channel, customer_name, customer_email, order_id, subject, complaint,
                 int(identity_verified), int(raised_by_staff),
                 json.dumps([{"role": "customer", "text": complaint}], ensure_ascii=False)),
            )
            ticket_id = f"TKT-{cur.lastrowid:04d}"
            self._db.execute("UPDATE tickets SET ticket_id = ? WHERE id = ?", (ticket_id, cur.lastrowid))
            self._db.commit()
        return {"ticket_id": ticket_id, "access_token": token, "created_at": now}

    def add_turn(self, ticket_id: str, role: str, text: str) -> None:
        """Append a chat turn. A new customer turn becomes the message to process and re-queues the ticket."""
        text = mask_sensitive(text)
        ticket = self.get(ticket_id)
        turns = json.loads(ticket.get("conversation") or "[]") if ticket else []
        turns.append({"role": role, "text": text})
        fields = {"conversation": json.dumps(turns, ensure_ascii=False)}
        if role == "customer":
            fields.update(complaint=text, state="queued")
        self.update_fields(ticket_id, **fields)

    def set_state(self, ticket_id: str, state: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE tickets SET state = ?, error = ?, updated_at = ? WHERE ticket_id = ?",
                (state, error, _now(), ticket_id),
            )
            self._db.commit()

    def update_fields(self, ticket_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE tickets SET {assignments} WHERE ticket_id = ?", (*fields.values(), ticket_id)
            )
            self._db.commit()

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None

    def list(
        self,
        desk_status: str | None = None,
        category: str | None = None,
        channel: str | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (
            ("desk_status", desk_status),
            ("category", category),
            ("channel", channel),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if search:
            clauses.append("(complaint LIKE ? OR customer_name LIKE ? OR order_id LIKE ? OR ticket_id LIKE ?)")
            params += [f"%{search}%"] * 4
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM tickets {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [_row_to_ticket(r) for r in rows]

    def customer_history(
        self, customer_email: str, order_id: str, exclude_ticket_id: str, days: int = 90, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Earlier processed tickets from the same email address or order ID, newest first.

        Matching is on email or order ID only, never on name, which is ambiguous.
        """
        email, order = customer_email.strip().lower(), order_id.strip().upper()
        if not email and not order:
            return []
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            rows = self._db.execute(
                "SELECT ticket_id, created_at, channel, category, urgency, status, desk_status, summary"
                " FROM tickets WHERE ticket_id != ? AND state IN ('completed', 'failed') AND created_at >= ?"
                " AND ((? != '' AND lower(customer_email) = ?) OR (? != '' AND upper(order_id) = ?))"
                " ORDER BY id DESC LIMIT ?",
                (exclude_ticket_id, since, email, email, order, order, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ ticket log (CSV)

    def write_log_row(self, row: dict[str, Any]) -> None:
        """Write a ticket's row to logs/tickets.csv: replace it if the ticket is already logged, else append."""
        with self._lock:
            if self.csv_path.exists():
                with self.csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
                    reader = csv.DictReader(fh)
                    rows = list(reader) if reader.fieldnames == LOG_COLUMNS else None
                if rows is not None and any(r["ticket_id"] == row["ticket_id"] for r in rows):
                    rows = [{c: row.get(c, "") for c in LOG_COLUMNS} if r["ticket_id"] == row["ticket_id"] else r
                            for r in rows]
                    tmp = self.csv_path.with_suffix(".tmp")
                    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
                        writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
                        writer.writeheader()
                        writer.writerows(rows)
                    tmp.replace(self.csv_path)  # atomic: readers never see a half-written log
                    return
            _append_csv(self.csv_path, LOG_COLUMNS, row, self._lock)

    def record_processed_ticket(self, ctx: "TicketContext") -> None:
        """Called by the Log Agent's log_ticket tool: persist the outcome and append the log row."""
        triage = ctx.triage
        decision = ctx.decision
        sources = [p.source for p in ctx.cited_passages] or [p.source for p in ctx.evidence]
        kb_sources = "; ".join(dict.fromkeys(sources)) if sources else "none"
        status = decision.status if decision else "Escalated"
        reason = "; ".join(decision.reasons) if decision else "Processing incomplete"
        confidence = ctx.confidence.score if ctx.confidence else 0.0

        self.update_fields(
            ctx.ticket_id,
            order_id=ctx.order_id,  # may have been found during processing (lookup by email)
            category=triage.category if triage else None,
            urgency=triage.urgency if triage else None,
            sentiment=triage.sentiment if triage else None,
            product=triage.product if triage else None,
            language=triage.language if triage else None,
            summary=triage.summary if triage else None,
            risk_flags=",".join(triage.risk_flags) if triage else None,
            missing_details="; ".join(triage.missing_details) if triage else None,
            kb_sources=kb_sources,
            passages_json=json.dumps([asdict(p) for p in ctx.passages], ensure_ascii=False),
            evidence_json=json.dumps(
                {"ids": ctx.evidence_ids, "no_match": ctx.no_match, "rationale": ctx.evidence_rationale,
                 "queries": ctx.search_queries, "cited_ids": ctx.cited_ids},
                ensure_ascii=False,
            ),
            response=ctx.customer_reply,
            response_annotated=ctx.reply,
            confidence=confidence,
            confidence_json=json.dumps(asdict(ctx.confidence), ensure_ascii=False) if ctx.confidence else None,
            status=status,
            escalation_reason=reason,
            agent_recommendation=ctx.agent_recommendation,
            desk_status=status,  # a new customer message re-opens a closed chat ticket with the new result
        )
        self.write_log_row(
            {
                "ticket_id": ctx.ticket_id,
                "timestamp": ctx.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "customer_name": ctx.customer_name,
                "order_id": ctx.order_id,
                "channel": ctx.channel,
                "complaint": ctx.complaint,
                "category": triage.category if triage else "",
                "urgency": triage.urgency if triage else "",
                "sentiment": triage.sentiment if triage else "",
                "product": (triage.product or "") if triage else "",
                "language": triage.language if triage else "",
                "kb_sources": kb_sources,
                "response": ctx.customer_reply,
                "confidence": f"{confidence:.2f}",
                "status": status,
                "escalation_reason": reason,
            }
        )

    # ------------------------------------------------------------------ staff actions

    def record_staff_action(
        self, ticket_id: str, action: str, desk_status: str, reviewer: str, final_reply: str = "", note: str = ""
    ) -> None:
        """Close or reopen a ticket; every action is also appended to logs/reviews.csv."""
        reviewed_at = _now()
        fields = dict(desk_status=desk_status, review_status=action, reviewed_by=reviewer,
                      reviewed_at=reviewed_at, review_note=note)
        if final_reply:
            fields["final_reply"] = final_reply
        self.update_fields(ticket_id, **fields)
        _append_csv(
            self.reviews_csv_path,
            REVIEW_COLUMNS,
            {
                "ticket_id": ticket_id,
                "reviewed_at": reviewed_at,
                "reviewer": reviewer,
                "action": action,
                "note": note,
                "final_reply": final_reply,
            },
            self._lock,
        )

    # ------------------------------------------------------------------ events

    def add_event(self, ticket_id: str, event_type: str, agent: str | None, payload: dict[str, Any]) -> dict:
        ts = datetime.now().isoformat(timespec="milliseconds")
        with self._lock:
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()[0]
            self._db.execute(
                "INSERT INTO events (ticket_id, seq, ts, type, agent, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, seq, ts, event_type, agent, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._db.commit()
        return {"ticket_id": ticket_id, "seq": seq, "ts": ts, "type": event_type, "agent": agent, "data": payload}

    def events(self, ticket_id: str, after_seq: int = 0) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM events WHERE ticket_id = ? AND seq > ? ORDER BY seq", (ticket_id, after_seq)
            ).fetchall()
        return [
            {
                "ticket_id": r["ticket_id"],
                "seq": r["seq"],
                "ts": r["ts"],
                "type": r["type"],
                "agent": r["agent"],
                "data": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ outbox

    def add_outbox(self, ticket_id: str, to_addr: str, subject: str, body: str, delivery: str, error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO outbox (ticket_id, to_addr, subject, body, created_at, delivery, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticket_id, to_addr, subject, body, _now(), delivery, error),
            )
            self._db.commit()

    def outbox(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def _append_csv(path: Path, columns: list[str], row: dict[str, Any], lock: threading.RLock) -> None:
    with lock:
        if path.exists():
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                header = next(csv.reader(fh), None)
            if header != columns:
                # Schema changed: keep the old file instead of mixing column layouts.
                path.rename(path.with_name(f"{path.stem}.{datetime.now():%Y%m%d%H%M%S}.bak{path.suffix}"))
        new_file = not path.exists()
        # utf-8-sig so Excel shows ₹ and non-English text correctly
        with path.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({c: row.get(c, "") for c in columns})


def _row_to_ticket(row: sqlite3.Row) -> dict[str, Any]:
    ticket = dict(row)
    for column, key in (
        ("passages_json", "passages"),
        ("evidence_json", "evidence"),
        ("confidence_json", "confidence_detail"),
    ):
        raw = ticket.pop(column, None)
        ticket[key] = json.loads(raw) if raw else None
    ticket.pop("id", None)
    return ticket
