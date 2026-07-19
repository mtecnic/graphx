"""RunEvent: the one stream every frontend consumes (CLI, TUI, SSE)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    SUPERSTEP_STARTED = "superstep_started"
    NODE_STARTED = "node_started"
    NODE_OUTPUT_CHUNK = "node_output_chunk"
    NODE_RETRYING = "node_retrying"
    NODE_FALLBACK = "node_fallback"
    NODE_REASK = "node_reask"
    NODE_CACHED = "node_cached"
    NODE_FINISHED = "node_finished"
    NODE_FAILED = "node_failed"
    STATE_UPDATED = "state_updated"
    INTERRUPT_RAISED = "interrupt_raised"
    RUN_RESUMED = "run_resumed"
    CHECKPOINT_SAVED = "checkpoint_saved"
    BUDGET_WARNING = "budget_warning"
    GUARD_TRIPPED = "guard_tripped"
    SUPERSTEP_FINISHED = "superstep_finished"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_CANCELLED = "run_cancelled"


class RunEvent(BaseModel):
    seq: int
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str
    thread_id: str
    step: int
    type: EventType
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


_SENTINEL: Any = None  # queue close marker


class EventBus:
    """Fan-out to per-subscriber queues, with bounded replay for late attach."""

    def __init__(self, run_id: str, thread_id: str, replay: int = 1000,
                 sink: Callable[[RunEvent], Awaitable[None]] | None = None):
        self.run_id = run_id
        self.thread_id = thread_id
        self._seq = 0
        self._queues: list[asyncio.Queue[RunEvent | None]] = []
        self._replay: deque[RunEvent] = deque(maxlen=replay)
        self._sink = sink
        self._closed = False

    async def emit(self, type_: EventType, *, step: int, node_id: str | None = None,
                   **data: Any) -> RunEvent:
        self._seq += 1
        event = RunEvent(seq=self._seq, run_id=self.run_id, thread_id=self.thread_id,
                         step=step, type=type_, node_id=node_id, data=data)
        self._replay.append(event)
        for queue in list(self._queues):
            queue.put_nowait(event)
        if self._sink is not None:
            await self._sink(event)
        return event

    def subscribe(self, from_seq: int = 0) -> AsyncIterator[RunEvent]:
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        for event in self._replay:
            if event.seq > from_seq:
                queue.put_nowait(event)
        if self._closed:
            queue.put_nowait(_SENTINEL)
        else:
            self._queues.append(queue)

        async def iterate() -> AsyncIterator[RunEvent]:
            try:
                while True:
                    event = await queue.get()
                    if event is _SENTINEL:
                        return
                    yield event
            finally:
                if queue in self._queues:
                    self._queues.remove(queue)

        return iterate()

    def close(self) -> None:
        self._closed = True
        for queue in list(self._queues):
            queue.put_nowait(_SENTINEL)
