"""subworkflow node: run another workflow file as a single node.

The child runs with the parent's services and an in-memory checkpointer
(child-level resume is not supported yet — the parent's checkpoint
simply re-runs the whole child). Child node events surface on the
parent bus as output chunks. Human gates inside a child are an error
for now; put gates in the parent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..engine.checkpoint import MemoryCheckpointer
from ..engine.errors import ConfigError, GraphxError
from ..engine.events import EventBus, EventType
from .registry import NodeContext, NodeResult, node_type


class SubworkflowConfig(BaseModel):
    workflow: str
    input: dict[str, Any] = Field(default_factory=dict)


class WaitConfig(BaseModel):
    seconds: float


@node_type("subworkflow", config_model=SubworkflowConfig)
async def subworkflow_node(ctx: NodeContext) -> NodeResult:
    from ..engine.executor import Executor
    from ..model.validate import has_errors, validate_graph
    from ..model.yaml_loader import load_graph
    from .registry import known_types

    config: SubworkflowConfig = ctx.config  # type: ignore[assignment]
    path = Path(config.workflow)
    if not path.is_absolute() and ctx.source_dir is not None:
        path = ctx.source_dir / path
    if not path.exists():
        raise ConfigError(f"subworkflow file not found: {path}")

    child_graph = load_graph(path)
    issues = validate_graph(child_graph, known_types=known_types())
    if has_errors(issues):
        raise ConfigError(f"subworkflow '{path}' is invalid: "
                          + "; ".join(str(i) for i in issues if i.severity == "error"))

    child_bus = EventBus(run_id=ctx.run_id, thread_id=f"{ctx.thread_id}:{ctx.node.id}")

    async def forward() -> None:
        async for event in child_bus.subscribe():
            if event.type in (EventType.NODE_STARTED, EventType.NODE_FINISHED,
                              EventType.NODE_FAILED):
                await ctx.emit_chunk(f"[{child_graph.name}] {event.type.value} "
                                     f"{event.node_id or ''}")

    import asyncio
    forward_task = asyncio.create_task(forward())
    executor = Executor(child_graph, MemoryCheckpointer(), child_bus, ctx.services)
    try:
        outcome = await executor.run(f"{ctx.thread_id}:{ctx.node.id}", dict(config.input))
    finally:
        child_bus.close()
        await forward_task

    if outcome.status == "interrupted":
        raise GraphxError(f"subworkflow '{child_graph.name}' hit a human gate "
                          "(not supported inside subworkflows yet)")
    if outcome.status != "finished":
        raise GraphxError(f"subworkflow '{child_graph.name}' {outcome.status}: "
                          f"{outcome.error or ''}")
    return NodeResult(output={"state": outcome.state, "steps": outcome.step})


@node_type("wait", config_model=WaitConfig)
async def wait_node(ctx: NodeContext) -> NodeResult:
    config: WaitConfig = ctx.config  # type: ignore[assignment]
    await ctx.services.clock.sleep(config.seconds)
    return NodeResult(output={"waited_s": config.seconds})
