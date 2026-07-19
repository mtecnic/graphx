"""Static workflow definition: Graph, NodeSpec, EdgeSpec, policies.

Everything here is frozen/declarative — behavior lives in the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

END = "end"


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base: float = 0.5
    factor: float = 2.0
    max_delay: float = 60.0
    jitter: bool = True
    retry_on: str = "transient"


@dataclass(frozen=True)
class Budget:
    tokens: int | None = None
    cost_usd: float | None = None
    deadline_s: float | None = None


@dataclass(frozen=True)
class StaticFallback:
    """Terminal degraded output at the end of a fallback chain."""
    output: Mapping[str, Any]


@dataclass(frozen=True)
class ResiliencePolicy:
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_s: float | None = None
    fallbacks: tuple[str | StaticFallback, ...] = ()
    validation_reasks: int = 2
    cache: bool = False
    budget: Budget | None = None


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str
    when: str | None = None
    kind: Literal["normal", "on_error"] = "normal"


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str
    config: Mapping[str, Any]
    resilience: ResiliencePolicy = field(default_factory=ResiliencePolicy)
    updates: Mapping[str, Any] = field(default_factory=dict)  # channel writes, may use <self.x>
    max_iterations: int | None = None      # per-thread visit guard (loop nodes)
    on_exhausted: str | None = None        # where to go when max_iterations trips


@dataclass(frozen=True)
class GraphConfig:
    max_steps: int = 25
    max_concurrency: int = 8
    budget: Budget | None = None


@dataclass(frozen=True)
class Graph:
    name: str
    channels: Mapping[str, Any]            # name -> ChannelSpec
    nodes: Mapping[str, NodeSpec]
    edges: tuple[EdgeSpec, ...]
    entry: tuple[str, ...]
    config: GraphConfig = field(default_factory=GraphConfig)
    mcp_servers: Mapping[str, Any] = field(default_factory=dict)
    providers: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""
    source_path: str | None = None

    def edges_from(self, node_id: str, kind: str = "normal") -> tuple[EdgeSpec, ...]:
        return tuple(e for e in self.edges if e.source == node_id and e.kind == kind)

    def edges_to(self, node_id: str, kind: str = "normal") -> tuple[EdgeSpec, ...]:
        return tuple(e for e in self.edges if e.target == node_id and e.kind == kind)
