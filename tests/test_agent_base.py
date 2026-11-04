"""FallbackClient: a second model provider is used only when the first is unavailable."""

import httpx
import openai
import pytest
from autogen_core.models import UserMessage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agents.base import FallbackClient


class DownClient(ReplayChatCompletionClient):
    def __init__(self, error: Exception) -> None:
        super().__init__(["unused"])
        self.error = error

    async def create(self, messages, **kwargs):
        raise self.error


MESSAGES = [UserMessage(content="hi", source="user")]
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


async def test_outage_switches_to_the_fallback_model():
    client = FallbackClient(DownClient(openai.APIConnectionError(request=REQUEST)), ReplayChatCompletionClient(["from gemini"]))
    assert (await client.create(MESSAGES)).content == "from gemini"


async def test_bad_requests_are_not_retried_on_the_fallback():
    error = openai.BadRequestError("invalid schema", response=httpx.Response(400, request=REQUEST), body=None)
    client = FallbackClient(DownClient(error), ReplayChatCompletionClient(["from gemini"]))
    with pytest.raises(openai.BadRequestError):
        await client.create(MESSAGES)


async def test_primary_is_used_when_available():
    client = FallbackClient(ReplayChatCompletionClient(["from openai"]), ReplayChatCompletionClient(["from gemini"]))
    assert (await client.create(MESSAGES)).content == "from openai"
