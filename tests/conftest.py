from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from graphx.engine.checkpoint import MemoryCheckpointer
from graphx.engine.errors import GraphxError, TransientError
from graphx.engine.events import EventBus, RunEvent
from graphx.engine.executor import Executor, RunOutcome
from graphx.engine.interrupts import Command
from graphx.engine.services import FakeClock, Services
from graphx.model.graph import (
    EdgeSpec, Graph, GraphConfig, NodeSpec, ResiliencePolicy, RetryPolicy,
)
from graphx.model.state import ChannelSpec
from graphx.nodes.registry import (
    NodeContext, NodeResult, load_builtin_nodes, node_type, known_types,
)

load_builtin_nodes()

# tiny importable modules for function/tool tests
import sys
import types

_div_mod = types.ModuleType("tests_div")
_div_mod.divide = lambda x: {"r": 10 / x}
sys.modules["tests_div"] = _div_mod

_tools_mod = types.ModuleType("tests_tools")
_tools_mod.word_count = lambda text: {"count": len(str(text).split())}
sys.modules["tests_tools"] = _tools_mod

# -------------------------------------------------------------- fake LLM


class ScriptedLLM:
    """Fake LLMClient: returns queued LLMResponses in order; records calls.

    Queue items may be plain strings (text responses) or LLMResponse
    objects (e.g. with tool_calls). If `by_model` is set, each model
    has its own queue.
    """

    def __init__(self, script: list | None = None,
                 by_model: dict[str, list] | None = None):
        from graphx.llm.client import LLMResponse, Usage
        self._response_cls = LLMResponse
        self._usage_cls = Usage
        self.script = list(script or [])
        self.by_model = {k: list(v) for k, v in (by_model or {}).items()}
        self.calls: list[dict] = []

    def _next(self, model: str):
        queue = self.by_model.get(model) if self.by_model else None
        if queue is None:
            queue = self.script
        if not queue:
            raise AssertionError(f"ScriptedLLM: no scripted response left for {model}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return self._response_cls(text=item, tool_calls=[],
                                      usage=self._usage_cls(10, 10), model=model)
        return item

    async def chat(self, model, messages, tools=None, stream_cb=None,
                   temperature=None, max_tokens=None, force_json=False):
        self.calls.append({"model": model, "messages": list(messages),
                           "tools": [t.name for t in tools] if tools else None})
        response = self._next(model)
        if stream_cb is not None and response.text and not response.tool_calls:
            await stream_cb(response.text)
        return response

    async def aclose(self):
        pass


# ---------------------------------------------------------------- test nodes

FLAKY_CALLS: dict[str, int] = {}


class TEmitConfig(BaseModel):
    value: Any = None
    channel: str | None = None


class TFlakyConfig(BaseModel):
    key: str
    fail_times: int
    transient: bool = True
    value: Any = "ok"


class TBoomConfig(BaseModel):
    transient: bool = False
    message: str = "boom"


class TSleepConfig(BaseModel):
    seconds: float


if "t_emit" not in known_types():

    @node_type("t_emit", config_model=TEmitConfig)
    async def t_emit(ctx: NodeContext) -> NodeResult:
        config: TEmitConfig = ctx.config
        updates = {config.channel: config.value} if config.channel else {}
        return NodeResult(output=config.value, updates=updates)

    @node_type("t_flaky", config_model=TFlakyConfig)
    async def t_flaky(ctx: NodeContext) -> NodeResult:
        config: TFlakyConfig = ctx.config
        calls = FLAKY_CALLS.get(config.key, 0)
        FLAKY_CALLS[config.key] = calls + 1
        if calls < config.fail_times:
            if config.transient:
                raise TransientError(f"flaky failure #{calls + 1}")
            raise GraphxError(f"deterministic failure #{calls + 1}")
        return NodeResult(output=config.value)

    @node_type("t_boom", config_model=TBoomConfig)
    async def t_boom(ctx: NodeContext) -> NodeResult:
        if ctx.config.transient:
            raise TransientError(ctx.config.message)
        raise GraphxError(ctx.config.message)

    @node_type("t_sleep", config_model=TSleepConfig)
    async def t_sleep(ctx: NodeContext) -> NodeResult:
        await ctx.services.clock.sleep(ctx.config.seconds)
        return NodeResult(output={"slept": ctx.config.seconds})


@pytest.fixture(autouse=True)
def _reset_flaky():
    FLAKY_CALLS.clear()
    yield


# ------------------------------------------------------------------ helpers

def node(id: str, type: str = "t_emit", *, retry: RetryPolicy | None = None,
         timeout: float | None = None, fallbacks=(), cache: bool = False,
         max_iterations: int | None = None, on_exhausted: str | None = None,
         updates: dict | None = None, **config: Any) -> NodeSpec:
    return NodeSpec(
        id=id, type=type, config=config,
        resilience=ResiliencePolicy(
            retry=retry or RetryPolicy(),
            timeout_s=timeout, fallbacks=tuple(fallbacks), cache=cache,
        ),
        updates=updates or {},
        max_iterations=max_iterations, on_exhausted=on_exhausted,
    )


def edge(source: str, target: str, when: str | None = None,
         kind: str = "normal") -> EdgeSpec:
    return EdgeSpec(source=source, target=target, when=when, kind=kind)


def graph(nodes: list[NodeSpec], edges: list[EdgeSpec], entry: list[str],
          channels: dict[str, ChannelSpec] | None = None,
          config: GraphConfig | None = None) -> Graph:
    return Graph(
        name="test", channels=channels or {},
        nodes={n.id: n for n in nodes}, edges=tuple(edges),
        entry=tuple(entry), config=config or GraphConfig(),
    )


def chan(name: str, reducer: str = "last", default: Any = None,
         type_: str = "any") -> ChannelSpec:
    return ChannelSpec(name=name, type_=type_, reducer=reducer, default=default)


class Harness:
    def __init__(self, g: Graph, services: Services | None = None,
                 checkpointer: MemoryCheckpointer | None = None):
        self.graph = g
        self.services = services or Services(clock=FakeClock())
        self.checkpointer = checkpointer or MemoryCheckpointer()
        self.events: list[RunEvent] = []

    async def run(self, input: dict | Command | None = None,
                  thread_id: str = "t1") -> RunOutcome:
        async def sink(event: RunEvent) -> None:
            self.events.append(event)

        bus = EventBus(run_id=f"r{len(self.events)}", thread_id=thread_id, sink=sink)
        executor = Executor(self.graph, self.checkpointer, bus, self.services)
        try:
            return await executor.run(thread_id, input if input is not None else {})
        finally:
            bus.close()

    def event_types(self) -> list[str]:
        return [e.type.value for e in self.events]

    def events_of(self, *types: str) -> list[RunEvent]:
        return [e for e in self.events if e.type.value in types]
