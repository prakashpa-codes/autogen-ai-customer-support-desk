"""Load a realistic set of demo tickets by running them through the real agents.

Each ticket goes through the same path as the live backend (ticket queue, four-agent team,
event timeline, ticket log), so the dashboard shows full timelines and confidence breakdowns.
Nothing is emailed: replies to email tickets land in the dashboard outbox.

Stop the backend first (both would write to the same database), then:

    python scripts/load_demo_data.py            # moves the current database and logs aside first
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.email_service import EmailService  # noqa: E402
from backend.events import EventBroker  # noqa: E402
from backend.review_service import ReviewService  # noqa: E402
from backend.ticket_service import TicketService  # noqa: E402
from config import settings  # noqa: E402
from core.orders import OrderBook  # noqa: E402
from core.retrieval import KnowledgeBase, OpenAIEmbedder  # noqa: E402
from core.store import TicketStore  # noqa: E402
from team import create_model_client, run_ticket  # noqa: E402

# (channel, name, email, order_id, messages). Several messages = one chat conversation.
# Email tickets arrive as from the mailbox (verified identity); staff tickets are raised from the desk.
DEMO = [
    ("Chat", "Ananya", "", "", ["hi", "What is your return policy for cricket shoes?", "ok thanks"]),
    ("Chat", "Arjun", "arjun.mehta@example.com", "", ["Where is my order ORD-49012?"]),
    ("Chat", "Vikram", "", "", ["What size bat should I buy for my 12 year old son? He is about 4 ft 10 in."]),
    ("Email", "Karthik", "karthik@example.com", "", ["Hi, I returned my running shoes last week. When will I get my refund?"]),
    ("Email", "Lakshmi", "lakshmi.n@example.com", "", ["Hello, what is the status of the refund for my order ORD-61299?"]),
    ("Email", "Rohit Das", "rohit.das@example.com", "", [
        "I ordered Eden Rubber Stud Cricket Shoes in UK 8 (order ORD-51007) but they are too tight. Can I swap them for UK 9?"]),
    ("Web Form", "Rahul Nair", "rahul.nair@example.com", "ORD-52001", [
        "My card was charged twice for this order. Please refund the extra payment."]),
    ("Chat", "Priya", "", "", ["I forgot my password and the OTP never arrives on my phone."]),
    ("Chat", "Meena", "", "", ["நான் வாங்கிய கிரிக்கெட் ஷூக்கள் சிறியதாக உள்ளன. பெரிய அளவுக்கு மாற்ற முடியுமா?"]),
    ("Chat", "Lakshmi Iyer", "lakshmi.iyer@example.com", "ORD-50733", [
        "My PowerBlend blender started smoking and sparking while I was using it, and my hand got slightly burnt."]),
    ("Web Form", "Sourav Basu", "sourav.basu@example.com", "", [
        "Do you sell gift cards for a company of 500 employees with custom branding?"]),
    ("Chat", "Coach Iqbal", "", "", ["We run a cricket academy. Can we order 25 kits with our logo, and is there a discount?"]),
    ("Email", "Arjun Mehta", "arjun.mehta@example.com", "ORD-49012", [
        "You charged my card three times! Fix this now or I'm contacting my bank and a lawyer."]),
    ("Chat", "Debashis", "", "", ["How long does delivery to Port Blair take?"]),
    ("Email", "Lakshmi", "lakshmi.n@example.com", "ORD-61299", ["Still no refund for order ORD-61299. It has been days."]),
    ("Staff", "Meera Iyer", "meera.iyer@example.com", "ORD-61234", ["Customer called: she wants to know the refund status for ORD-61234."]),
]

# Staff actions after processing, as (ticket number in DEMO order, reply or "", note).
STAFF_ACTIONS = [
    (13, "Hello Arjun, I'm sorry about the repeated charges. Our payments team has confirmed one charge is the "
         "order and the other two are authorisation holds that your bank releases within 5-7 business days. "
         "If they are still showing after that, reply to this email and we will raise it with the bank for you.",
     "Checked with payments: two holds, no capture."),
    (11, "", "Corporate gifting is handled by the partnerships team; forwarded to them."),
]


def archive_current_data() -> None:
    """Move the existing database and logs aside (nothing is deleted)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path in [settings.db_path, settings.db_path.with_name(settings.db_path.name + "-wal"),
                 settings.db_path.with_name(settings.db_path.name + "-shm")]:
        if path.exists():
            shutil.move(path, path.with_name(f"{path.name}.bak-{stamp}"))
    for name in ("tickets.csv", "reviews.csv"):
        path = settings.log_dir / name
        if path.exists():
            shutil.move(path, settings.log_dir / f"{path.stem}-bak-{stamp}.csv")
    print(f"Moved the old database and logs aside (suffix {stamp}).")


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    archive_current_data()
    kb = KnowledgeBase(settings.kb_dir, OpenAIEmbedder(settings.embedding_model, settings.openai_api_key), settings.index_dir).build()
    orders = OrderBook.load(settings.orders_path)
    store = TicketStore(settings.db_path, settings.log_dir)
    email_service = EmailService(store)  # no Gmail sender: replies go to the outbox

    async def pipeline(ctx, emit):
        client = create_model_client(settings)
        try:
            return await run_ticket(ctx, kb, settings, store.record_processed_ticket, emit, client, orders=orders)
        finally:
            await client.close()

    tickets = TicketService(store, EventBroker(store), settings, pipeline, email_service.on_ticket_completed)
    review = ReviewService(settings, store, email_service, lambda: create_model_client(settings))
    await tickets.start()

    async def wait(ticket_id: str) -> dict:
        while (t := store.get(ticket_id))["state"] not in {"completed", "failed"}:
            await asyncio.sleep(0.5)
        return t

    ids = []
    for n, (channel, name, email, order_id, messages) in enumerate(DEMO, start=1):
        staff = channel == "Staff"
        created = await tickets.submit(
            complaint=messages[0], channel="Chat" if staff else channel, customer_name=name, order_id=order_id,
            customer_email=email, identity_verified=channel == "Email" or staff, raised_by_staff=staff,
        )
        t = await wait(created["ticket_id"])
        for follow_up in messages[1:]:  # later chat messages continue the same ticket
            await tickets.continue_conversation(t["ticket_id"], follow_up)
            t = await wait(t["ticket_id"])
        ids.append(t["ticket_id"])
        print(f"{n:2}. {t['ticket_id']} {channel:8} {t['desk_status']:9} {t.get('category') or '':8} {(t.get('escalation_reason') or '')[:70]}")

    for number, reply, note in STAFF_ACTIONS:
        ticket_id = ids[number - 1]
        if store.get(ticket_id)["desk_status"] == "Escalated":
            await review.close(ticket_id, "support", reply, note)
            print(f"Closed {ticket_id}" + (" with a reply" if reply else " without a reply"))

    await tickets.stop()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
