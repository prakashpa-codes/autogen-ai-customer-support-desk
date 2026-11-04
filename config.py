"""Central configuration, loaded once from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development"))

    # Models
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-3-small"))
    # Optional fallback chat model, used only when OpenAI is unavailable
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))

    # Resolution policy
    confidence_threshold: float = field(default_factory=lambda: _float("CONFIDENCE_THRESHOLD", 0.70))
    min_relevance: float = field(default_factory=lambda: _float("MIN_RELEVANCE", 0.40))
    top_k: int = field(default_factory=lambda: _int("TOP_K", 5))
    team_mode: str = field(default_factory=lambda: _env("TEAM_MODE", "round_robin"))

    # Backend service
    max_concurrent_tickets: int = field(default_factory=lambda: _int("MAX_CONCURRENT_TICKETS", 2))
    public_rate_limit: int = field(default_factory=lambda: _int("PUBLIC_RATE_LIMIT_PER_MINUTE", 6))
    max_message_chars: int = field(default_factory=lambda: _int("MAX_MESSAGE_CHARS", 4000))
    desk_url: str = field(default_factory=lambda: _env("DESK_URL", "http://localhost:8501/dashboard"))

    # Staff authentication
    desk_username: str = field(default_factory=lambda: _env("DESK_USERNAME", "support"))
    desk_password_hash: str = field(default_factory=lambda: _env("DESK_PASSWORD_HASH"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    token_ttl_hours: int = field(default_factory=lambda: _int("TOKEN_TTL_HOURS", 12))

    # Gmail channel through the Gmail MCP server (mcp_servers/gmail_server.py); off unless enabled
    gmail_enabled: bool = field(default_factory=lambda: _env("GMAIL_ENABLED", "false").lower() == "true")
    gmail_poll_seconds: int = field(default_factory=lambda: _int("GMAIL_POLL_SECONDS", 60))

    # Paths
    kb_dir: Path = ROOT / "knowledge_base"
    log_dir: Path = field(default_factory=lambda: Path(_env("LOG_DIR", str(ROOT / "logs"))))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))
    index_dir: Path = field(default_factory=lambda: Path(_env("INDEX_DIR", str(ROOT / "faiss_index"))))
    catalogue_path: Path = ROOT / "data" / "catalogue.json"
    orders_path: Path = ROOT / "data" / "orders.json"
    storefront_dir: Path = ROOT / "storefront"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "resolvedesk.db"


settings = Settings()
