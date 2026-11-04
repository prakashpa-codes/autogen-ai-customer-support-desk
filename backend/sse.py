"""Server-Sent Events formatting."""

from __future__ import annotations

import json
from typing import AsyncIterator

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


async def sse_stream(events: AsyncIterator[dict | None]) -> AsyncIterator[str]:
    """Format {seq, type, data} dicts as SSE frames; None becomes a keep-alive comment."""
    async for event in events:
        if event is None:
            yield ": keep-alive\n\n"
            continue
        payload = json.dumps(event["data"], ensure_ascii=False, default=str)
        yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {payload}\n\n"
