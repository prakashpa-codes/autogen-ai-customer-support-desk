from datetime import date
from pathlib import Path

from core.orders import OrderBook

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 3)


def book() -> OrderBook:
    return OrderBook.load(ROOT / "data" / "orders.json", today=TODAY)


def test_orders_load_with_dates_relative_to_today():
    b = book()
    assert len(b) >= 10
    record = b.lookup("ORD-48213", "", staff=True).record
    assert "Delivered 28 Oct 2026 (6 days ago)" in record  # -6 days from 3 Nov 2026
    assert "₹3,299" in record and "UPI" in record


def test_access_rules():
    b = book()
    assert b.lookup("ord-48213", "", staff=True).status == "found"  # staff, any case
    assert b.lookup("ORD-48213", "Ananya.Rao@example.com", staff=False).status == "found"  # matching email
    assert b.lookup("ORD-48213", "someone@example.com", staff=False).status == "not_verified"
    assert b.lookup("ORD-48213", "", staff=False).status == "not_verified"
    hidden = b.lookup("ORD-48213", "someone@example.com", staff=False)
    assert hidden.record == ""  # nothing about the order leaks
    assert b.lookup("ORD-00000", "", staff=True).status == "not_found"


def test_return_and_refund_progress_is_rendered():
    record = book().lookup("ORD-61299", "lakshmi.n@example.com", staff=False).record
    assert "Quality check passed" in record and "ARN 74120461299" in record


def test_orders_for_email():
    assert book().orders_for_email("KARTHIK@example.com") == ["ORD-50689"]
