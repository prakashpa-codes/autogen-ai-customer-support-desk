"""A thin AssistantAgent subclass that hands each final response to a callback.

The callback writes the agent's result into the shared TicketContext the moment the agent
finishes, before the next agent starts, so tools later in the pipeline always see it.
It may also return a replacement Response, which the Knowledge Base Agent uses to publish
its evidence pack verbatim from the index (the LLM chooses passages; code renders them).
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator, Callable, Sequence

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage
from autogen_core import CancellationToken
from autogen_core.models import CreateResult, FunctionExecutionResultMessage, RequestUsage
from autogen_core.tools import FunctionTool

log = logging.getLogger(__name__)

ResponseHook = Callable[[Response], Response | None]


class RequiredToolClient:
    """Wraps a model client so the model must call one of the offered tools (tool_choice="required").

    Used for agents whose turn must end in a tool call (submit_resolution, log_ticket): a prompt
    can ask for that, but only the API setting guarantees it. With done_marker set, once a tool
    result starting with that marker is in the context the turn is finished, so the wrapper ends
    it locally instead of paying for another model call. Everything else, including token usage,
    is delegated to the shared client.
    """

    def __init__(self, inner, done_marker: str | None = None) -> None:
        self._inner = inner
        self._done_marker = done_marker

    def _finished(self, messages) -> bool:
        if not self._done_marker or not messages or not isinstance(messages[-1], FunctionExecutionResultMessage):
            return False
        return any(str(r.content).startswith(self._done_marker) for r in messages[-1].content)

    async def create(self, messages, *, tools=(), tool_choice="auto", **kwargs):
        if self._finished(messages):
            return CreateResult(finish_reason="stop", content="Done.", usage=RequestUsage(0, 0), cached=False)
        return await self._inner.create(messages, tools=tools, tool_choice="required" if tools else tool_choice, **kwargs)

    def create_stream(self, messages, *, tools=(), tool_choice="auto", **kwargs):
        return self._inner.create_stream(
            messages, tools=tools, tool_choice="required" if tools else tool_choice, **kwargs
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class FallbackClient:
    """Sends each model call to the primary client; if that provider is unavailable, to the fallback.

    "Unavailable" means rate limits or exhausted quota, connection errors, timeouts and server
    errors. Bad requests are not retried elsewhere: they are bugs, and would fail there too.
    """

    def __init__(self, primary, fallback) -> None:
        import openai

        self._primary = primary
        self._fallback = fallback
        self._retry_on = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)

    async def create(self, messages, **kwargs):
        try:
            return await self._primary.create(messages, **kwargs)
        except self._retry_on as exc:
            log.warning("Primary model unavailable (%s); using the fallback model", type(exc).__name__)
            return await self._fallback.create(messages, **kwargs)

    def create_stream(self, messages, **kwargs):
        return self._primary.create_stream(messages, **kwargs)

    def total_usage(self):
        a, b = self._primary.total_usage(), self._fallback.total_usage()
        return RequestUsage(a.prompt_tokens + b.prompt_tokens, a.completion_tokens + b.completion_tokens)

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()

    def __getattr__(self, name: str):
        return getattr(self._primary, name)


def strict_tool(func: Callable) -> FunctionTool:
    """Wrap a tool so OpenAI enforces its argument schema exactly ("strict" function calling).

    Strict tools are required when an agent combines tools with structured output, and they
    guarantee the model's arguments always match the Python signature.
    """
    description = (func.__doc__ or func.__name__).strip().split("\n")[0]
    return FunctionTool(func, description=description, strict=True)


class RecordingAssistantAgent(AssistantAgent):
    def __init__(self, *args, on_response: ResponseHook | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._on_response = on_response

    async def on_messages_stream(
        self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken
    ) -> AsyncGenerator[BaseAgentEvent | BaseChatMessage | Response, None]:
        async for item in super().on_messages_stream(messages, cancellation_token):
            if isinstance(item, Response) and self._on_response is not None:
                item = self._on_response(item) or item
            yield item
