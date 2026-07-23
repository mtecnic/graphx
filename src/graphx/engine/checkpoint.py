"""Checkpoint = full snapshot per superstep. Resume = load + continue.

No event sourcing, no deterministic replay: crash or cancel loses at
most the current superstep, which simply re-runs on resume.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Protocol
from uuid import uuid4

from .interrupts import Interrupt


@dataclass(frozen=True)
class TaskSpec:
    """One unit of work in a frontier: a node, optionally with a Send payload."""
    node_id: str
    item: Any = None                     # Send payload, exposed as <item.*>
    collect_channel: str | None = None   # append this task's output to a channel
    task_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True)
class DeadLetter:
    node_id: str
    attempts: int
    error: str
    error_type: str
    step: int


@dataclass
class RunMetrics:
    tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    nodes_run: int = 0


@dataclass(frozen=True)
class Checkpoint:
    thread_id: str
    run_id: str
    step: int
    graph_name: str
    state: dict[str, Any]
    frontier: tuple[TaskSpec, ...]
    node_outputs: dict[str, Any]
    visits: dict[str, int]
    join_arrivals: dict[str, dict[str, str]]   # merge node -> {source: ok|failed|skipped}
    pending_interrupt: Interrupt | None
    metrics: RunMetrics
    dead_letters: tuple[DeadLetter, ...]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Checkpoint:
        interrupt = data.get("pending_interrupt")
        return cls(
            thread_id=data["thread_id"],
            run_id=data["run_id"],
            step=data["step"],
            graph_name=data["graph_name"],
            state=data["state"],
            frontier=tuple(TaskSpec(**t) for t in data["frontier"]),
            node_outputs=data["node_outputs"],
            visits=data["visits"],
            join_arrivals=data["join_arrivals"],
            pending_interrupt=Interrupt(**interrupt) if interrupt else None,
            metrics=RunMetrics(**data["metrics"]),
            dead_letters=tuple(DeadLetter(**d) for d in data["dead_letters"]),
        )


@dataclass(frozen=True)
class CheckpointMeta:
    thread_id: str
    run_id: str
    step: int
    saved_at: str


class Checkpointer(Protocol):
    async def save(self, ckpt: Checkpoint) -> None: ...
    async def load_latest(self, thread_id: str) -> Checkpoint | None: ...
    async def history(self, thread_id: str) -> list[CheckpointMeta]: ...
    async def cache_get(self, node_id: str, input_hash: str) -> dict[str, Any] | None: ...
    async def cache_put(self, node_id: str, input_hash: str, output: dict[str, Any]) -> None: ...


class MemoryCheckpointer:
    """In-process checkpointer for tests and throwaway runs."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    async def save(self, ckpt: Checkpoint) -> None:
        self._checkpoints.setdefault(ckpt.thread_id, []).append(ckpt)

    async def load_latest(self, thread_id: str) -> Checkpoint | None:
        history = self._checkpoints.get(thread_id)
        return history[-1] if history else None

    async def history(self, thread_id: str) -> list[CheckpointMeta]:
        return [CheckpointMeta(c.thread_id, c.run_id, c.step, "")
                for c in self._checkpoints.get(thread_id, [])]

    async def cache_get(self, node_id: str, input_hash: str) -> dict[str, Any] | None:
        return self._cache.get((node_id, input_hash))

    async def cache_put(self, node_id: str, input_hash: str, output: dict[str, Any]) -> None:
        self._cache[(node_id, input_hash)] = output
