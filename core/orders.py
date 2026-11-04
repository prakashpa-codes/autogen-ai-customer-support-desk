"""Order lookup.

Dada Sports orders live in data/orders.json (fictional data, dates stored as day offsets from
today). OrderBook renders an order as a plain-text record that the agents can cite like a
policy passage, so amounts and dates in a reply are fact-checked against it.

Access rule: order details are personal, and an order ID alone is guessable. Details are
released only for tickets raised by staff, or when the ticket's email matches the order's
email (the same check "track my order" pages use). Otherwise the lookup confirms nothing
beyond asking the customer to confirm the email address used for the order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def _inr(amount: int) -> str:
    digits = str(amount)
    if len(digits) <= 3:
        return f"₹{digits}"
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return f"₹{','.join(([head] if head else []) + groups)},{tail}"


@dataclass
class LookupResult:
    status: str  # found | not_verified | not_found
    order_id: str
    record: str = ""  # rendered order facts, only when status == "found"


class OrderBook:
    def __init__(self, orders: list[dict[str, Any]], today: date | None = None) -> None:
        self.today = today or date.today()
        self._orders = {o["order_id"].upper(): o for o in orders}

    @classmethod
    def load(cls, path: Path, today: date | None = None) -> "OrderBook":
        return cls(json.loads(path.read_text(encoding="utf-8"))["orders"], today)

    def __len__(self) -> int:
        return len(self._orders)

    def _day(self, offset: int | None) -> str:
        return (self.today + timedelta(days=offset)).strftime("%d %b %Y") if offset is not None else ""

    def _ago(self, offset: int) -> str:
        days = -offset
        return "today" if days == 0 else ("yesterday" if days == 1 else f"{days} days ago")

    def lookup(self, order_id: str, email: str, staff: bool) -> LookupResult:
        order = self._orders.get(order_id.strip().upper())
        if order is None:
            return LookupResult("not_found", order_id.strip().upper())
        if not staff and (not email or email.strip().lower() != order["email"].lower()):
            return LookupResult("not_verified", order["order_id"])
        return LookupResult("found", order["order_id"], self.render(order))

    def orders_for_email(self, email: str) -> list[str]:
        email = email.strip().lower()
        return [o["order_id"] for o in self._orders.values() if email and o["email"].lower() == email]

    def render(self, o: dict[str, Any]) -> str:
        lines = [
            f"Order {o['order_id']}, placed {self._day(o['placed'])} ({self._ago(o['placed'])}), paid by {o['payment_method']}.",
            f"Current status: {o['status']}. Delivery to {o['city']}.",
        ]
        for item in o["items"]:
            variant = f", {item['variant']}" if item.get("variant") else ""
            lines.append(f"Item: {item['qty']} x {item['name']}{variant}, {_inr(item['price'])}.")
        total = sum(i["price"] * i["qty"] for i in o["items"]) + o.get("shipping_fee", 0)
        fee = f" including {_inr(o['shipping_fee'])} shipping/handling" if o.get("shipping_fee") else ""
        lines.append(f"Order total: {_inr(total)}{fee}.")
        if o.get("payments"):
            lines.append(f"Payment record: {o['payments']}.")

        s = o.get("shipment") or {}
        if s.get("courier"):
            lines.append(f"Shipped with {s['courier']} (tracking {s['tracking']}), dispatched {self._day(s.get('dispatched'))}.")
        if s.get("delivered") is not None:
            lines.append(f"Delivered {self._day(s['delivered'])} ({self._ago(s['delivered'])}).")
        elif s.get("edd") is not None:
            edd_offset = s["edd"]
            when = "today" if edd_offset == 0 else (f"{-edd_offset} days ago" if edd_offset < 0 else f"in {edd_offset} days")
            lines.append(f"Estimated delivery date: {self._day(edd_offset)} ({when}).")
            if s.get("last_scan"):
                lines.append(f"Last tracking update: {s['last_scan']}, {self._day(s.get('last_scan_day'))} ({self._ago(s['last_scan_day'])}).")

        r = o.get("return")
        if r:
            lines.append(f"Return requested {self._day(r['requested'])} (reason: {r['reason']}); "
                         f"picked up {self._day(r['picked_up'])} ({self._ago(r['picked_up'])}).")
            if r.get("qc_passed") is not None:
                lines.append(f"Quality check passed {self._day(r['qc_passed'])}.")
            else:
                lines.append("Quality check not completed yet.")
            refund = r["refund"]
            if r.get("refund_initiated") is not None:
                refund += f", on {self._day(r['refund_initiated'])} ({self._ago(r['refund_initiated'])})"
            lines.append(f"Refund: {refund}.")
        return "\n".join(lines)
