"""Node-type plugin contract.

A node type is an async handler plus a pydantic config model:

    class ShellConfig(BaseModel):
        command: list[str]
        ...

    @node_type("shell", config_model=ShellConfig)
    async def shell_node(ctx: NodeContext) -> NodeResult: ...

The loader keeps node config as a raw mapping; the executor validates
it against the registered model just before execution (after <ref>
resolution, so refs can appear anywhere in the config).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn

from pydantic import BaseModel

from ..engine.errors import InterruptSignal
from ..engine.services import Services
from ..model.graph import NodeSpec


@dataclass(frozen=True)
class Send:
    """Dynamic fan-out: schedule `target` next superstep with an item payload."""
    target: str
    payload: Any = None
    collect_channel: str | None = None


@dataclass
class NodeMetrics:
    duration_s: float = 0.0
    attempts: int = 1
    tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    degraded: bool = False


@dataclass
class NodeResult:
    output: Any = None                                  # addressable as <node.field>
    updates: Mapping[str, Any] = field(default_factory=dict)
    goto: tuple[str, ...] | None = None                 # overrides edge routing
    sends: tuple[Send, ...] = ()
    metrics: NodeMetrics = field(default_factory=NodeMetrics)


ChunkEmitter = Callable[[str], Awaitable[None]]


@dataclass
class NodeContext:
    node: NodeSpec
    config: BaseModel                    # validated, refs resolved
    state_values: Mapping[str, Any]
    node_outputs: Mapping[str, Any]
    services: Services
    thread_id: str
    run_id: str
    step: int
    visit: int = 1                       # nth execution of this node in the thread
    item: Any = None                     # Send payload for this task
    resume_value: Any = None             # set when resuming into an interrupted node
    model_override: str | None = None    # set by the fallback layer
    source_dir: Any = None               # Path of the workflow file's directory
    _emit_chunk: ChunkEmitter | None = None

    async def emit_chunk(self, chunk: str) -> None:
        if self._emit_chunk is not None:
            await self._emit_chunk(chunk)

    def interrupt(self, payload: Any) -> NoReturn:
        raise InterruptSignal(payload)


NodeHandler = Callable[[NodeContext], Awaitable[NodeResult]]


@dataclass(frozen=True)
class NodeType:
    name: str
    handler: NodeHandler
    config_model: type[BaseModel]
    defer_keys: frozenset[str] = frozenset()  # config keys whose <refs> resolve later
                                              # (e.g. a map's inline template uses <item.*>)


_REGISTRY: dict[str, NodeType] = {}


def node_type(name: str, *, config_model: type[BaseModel],
              defer: tuple[str, ...] = ()) -> Callable[[NodeHandler], NodeHandler]:
    def register(handler: NodeHandler) -> NodeHandler:
        if name in _REGISTRY:
            raise ValueError(f"node type '{name}' already registered")
        _REGISTRY[name] = NodeType(name=name, handler=handler, config_model=config_model,
                                   defer_keys=frozenset(defer))
        return handler
    return register


def get_node_type(name: str) -> NodeType:
    if name not in _REGISTRY:
        raise KeyError(f"unknown node type '{name}' (have: {', '.join(sorted(_REGISTRY))})")
    return _REGISTRY[name]


def known_types() -> set[str]:
    return set(_REGISTRY)


def load_builtin_nodes() -> None:
    """Import built-in node modules so their decorators run."""
    from . import (  # noqa: F401
        agent, api, flow, function, human, mcp, router, shell, subworkflow,
    )
