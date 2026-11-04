"""Shared fixtures. Everything here runs offline: no API key, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, RequestUsage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from core.retrieval import HashingEmbedder, KnowledgeBase  # noqa: E402
from core.store import TicketStore  # noqa: E402


@pytest.fixture(scope="session")
def kb() -> KnowledgeBase:
    return KnowledgeBase(ROOT / "knowledge_base", HashingEmbedder()).build()


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    # The hashing embedder produces lower similarities than real embeddings, so the gate is lower.
    return Settings(
        openai_api_key="test",
        gemini_api_key="",  # never call a real model provider
        gmail_enabled=False,  # never touch the real mailbox, whatever .env says
        min_relevance=0.05,
        confidence_threshold=0.70,
        top_k=5,
        team_mode="round_robin",
        log_dir=tmp_path / "logs",
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def store(test_settings: Settings) -> TicketStore:
    s = TicketStore(test_settings.db_path, test_settings.log_dir)
    yield s
    s.close()


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> CreateResult:
    return CreateResult(
        finish_reason="function_calls",
        content=[FunctionCall(id=call_id, name=name, arguments=json.dumps(arguments))],
        usage=RequestUsage(prompt_tokens=10, completion_tokens=5),
        cached=False,
    )


def text(content: str | dict) -> CreateResult:
    if isinstance(content, dict):
        content = json.dumps(content)
    return CreateResult(
        finish_reason="stop", content=content, usage=RequestUsage(prompt_tokens=10, completion_tokens=5), cached=False
    )
