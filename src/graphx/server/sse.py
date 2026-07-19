"""SSE bridge (sse-starlette)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sse_starlette.sse import EventSourceResponse


def sse_response(generator: AsyncIterator[dict[str, Any]]) -> EventSourceResponse:
    return EventSourceResponse(generator, ping=15)
