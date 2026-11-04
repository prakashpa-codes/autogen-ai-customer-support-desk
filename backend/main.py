"""ResolveDesk backend (FastAPI).

Run locally:   uvicorn backend.main:app --port 8000
Serves:        /api/v1/...      JSON + SSE API
               /                storefront (static site in storefront/)
               /docs            OpenAPI documentation
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend import routes_public, routes_staff
from backend.deps import Services
from backend.email_service import EmailService
from backend.events import EventBroker
from backend.gmail_channel import GmailChannel
from backend.review_service import ReviewService
from backend.security import RateLimiter
from backend.ticket_service import Pipeline, TicketService
from config import Settings, settings as default_settings
from core.orders import OrderBook
from core.retrieval import KnowledgeBase, OpenAIEmbedder
from core.store import TicketStore
from team import create_model_client, run_ticket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# AutoGen logs every message payload at INFO (complaints, replies, customer details). The
# ticket store already keeps that trace, so keep it out of the service logs.
for noisy in ("autogen_core", "autogen_core.events", "autogen_agentchat", "httpx", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("resolvedesk")


def _warm_tokenizer(settings: Settings) -> None:
    """Load tiktoken's encoding once at startup.

    AutoGen's OpenAI client counts tokens with tiktoken. Its first load reads (and, without a
    cache, downloads) the encoding file synchronously, which took over 15 s in testing. Doing
    it here, in a thread, keeps that cost out of the first customer request, where it would
    block the whole event loop.
    """
    import tiktoken

    try:
        tiktoken.encoding_for_model(settings.model)
    except KeyError:  # model unknown to tiktoken: AutoGen falls back to this encoding
        tiktoken.get_encoding("o200k_base")


def _secret_key(settings: Settings) -> str:
    if settings.secret_key:
        return settings.secret_key
    if settings.app_env == "production":
        raise RuntimeError("SECRET_KEY must be set in production.")
    log.warning("SECRET_KEY not set: using a temporary key (staff sessions end when the backend restarts).")
    return secrets.token_urlsafe(32)


def create_app(
    settings: Settings | None = None,
    kb: KnowledgeBase | None = None,
    pipeline: Pipeline | None = None,
    model_client_factory: Callable | None = None,
) -> FastAPI:
    """App factory. Tests inject an offline knowledge base, pipeline and model client."""
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.desk_password_hash:
            log.warning("DESK_PASSWORD_HASH is not set: staff sign-in is disabled. Run scripts/hash_password.py.")
        if model_client_factory is None:  # real OpenAI client (tests inject their own and stay offline)
            # Keep tiktoken's downloaded encoding on disk so later starts skip the download.
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(settings.data_dir / "tiktoken"))
            await asyncio.to_thread(_warm_tokenizer, settings)
        knowledge = kb
        if knowledge is None:
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not set. Copy .env.example to .env and add your key.")
            knowledge = KnowledgeBase(
                settings.kb_dir, OpenAIEmbedder(settings.embedding_model, settings.openai_api_key), settings.index_dir
            ).build()

        orders = OrderBook.load(settings.orders_path)
        store = TicketStore(settings.db_path, settings.log_dir)
        broker = EventBroker(store)
        email_service = EmailService(store)
        client_factory = model_client_factory or (lambda: create_model_client(settings))

        async def agent_pipeline(ctx, emit) -> dict[str, Any]:
            client = client_factory()
            try:
                return await run_ticket(ctx, knowledge, settings, store.record_processed_ticket, emit, client, orders=orders)
            finally:
                await client.close()

        tickets = TicketService(store, broker, settings, pipeline or agent_pipeline, email_service.on_ticket_completed)
        app.state.services = Services(
            settings=settings,
            store=store,
            kb=knowledge,
            broker=broker,
            tickets=tickets,
            email=email_service,
            review=ReviewService(settings, store, email_service, client_factory),
            limiter=RateLimiter(settings.public_rate_limit),
            secret_key=_secret_key(settings),
            catalogue=json.loads(settings.catalogue_path.read_text(encoding="utf-8")),
            model_client_factory=client_factory,
        )
        await tickets.start()
        gmail = None
        if settings.gmail_enabled:
            gmail = GmailChannel(email_service, tickets, store, client_factory, settings.gmail_poll_seconds)
            try:
                await gmail.start()
            except Exception:  # the desk still works; replies stay in the outbox
                log.exception("Gmail channel could not start. Run scripts/gmail_auth.py if the sign-in expired.")
                await gmail.stop()
                gmail = None
        app.state.services.gmail = gmail
        log.info("ResolveDesk ready: %d KB chunks, model %s, team mode %s", len(knowledge.chunks), settings.model, settings.team_mode)
        yield
        if gmail:
            await gmail.stop()
        await tickets.stop()
        store.close()

    app = FastAPI(
        title="ResolveDesk API",
        description="Multi-agent customer support backend for Dada Sports.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(routes_public.router)
    app.include_router(routes_staff.router)

    @app.get("/api/v1/health", tags=["health"])
    def health() -> dict:
        services: Services = app.state.services
        return {
            "status": "ok",
            "model": settings.model,
            "team_mode": settings.team_mode,
            "kb_chunks": len(services.kb.chunks),
            "queue_depth": services.tickets.queue_depth,
        }

    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> RedirectResponse:
        # Behind the reverse proxy /dashboard is routed to the desk directly; locally this redirects.
        return RedirectResponse(settings.desk_url)

    if settings.storefront_dir.exists():
        app.mount("/", StaticFiles(directory=settings.storefront_dir, html=True), name="storefront")
    return app


app = create_app()
