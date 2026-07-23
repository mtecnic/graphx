"""Flow nodes: condition (branching + loops), map (fan-out), merge (join).

map runs its inline template node once per item *within one superstep*
(atomic): natural barrier semantics, simple edges, coarse checkpoint.
The template's <item.X> refs resolve against {item_as: value}. Collected
outputs are written to `collect.channel` — declare that channel with
reducer `extend` (with `append` you'd get one nested list per map run).

merge is scheduled by the engine once every incoming branch has settled
(success, failure, or condition-skipped); it fails — routing on_error if
present — when successes < success_threshold.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..engine.errors import ConfigError, GraphxError
from ..model.refs import RefContext, resolve
from .registry import NodeContext, NodeResult, get_node_type, node_type


# --------------------------------------------------------------- condition

class Branch(BaseModel):
    if_: str | None = Field(default=None, alias="if")
    goto: str | None = None
    else_: str | None = Field(default=None, alias="else")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check(self) -> "Branch":
        if self.else_ is not None:
            return self
        if self.if_ is None or self.goto is None:
            raise ValueError("branch needs either {if: expr, goto: node} or {else: node}")
        return self


class ConditionConfig(BaseModel):
    branches: list[Branch]


@node_type("condition", config_model=ConditionConfig)
async def condition_node(ctx: NodeContext) -> NodeResult:
    from ..model.exprs import build_namespace, evaluate
    config: ConditionConfig = ctx.config  # type: ignore[assignment]
    namespace = build_namespace(ctx.state_values, ctx.node_outputs)
    for branch in config.branches:
        if branch.else_ is not None:
            return NodeResult(output={"taken": branch.else_}, goto=(branch.else_,))
        if evaluate(branch.if_, namespace):
            return NodeResult(output={"taken": branch.goto}, goto=(branch.goto,))
    return NodeResult(output={"taken": None}, goto=())  # no branch matched: end here


# --------------------------------------------------------------------- map

class CollectSpec(BaseModel):
    channel: str


class MapConfig(BaseModel):
    over: Any
    item_as: str = "item"
    node: dict[str, Any]
    max_concurrency: int = 4
    collect: CollectSpec | None = None
    on_item_error: str = "fail"      # "fail" | "skip"


@node_type("map", config_model=MapConfig, defer=("node",))
async def map_node(ctx: NodeContext) -> NodeResult:
    config: MapConfig = ctx.config  # type: ignore[assignment]
    items = config.over
    if not isinstance(items, list):
        raise ConfigError(f"map.over must resolve to a list, got {type(items).__name__}")

    template = dict(config.node)
    template_type = get_node_type(str(template.pop("type", "")))
    semaphore = asyncio.Semaphore(config.max_concurrency)
    results: list[Any] = [None] * len(items)
    ok: list[bool] = [False] * len(items)      # per-index success (None is a valid output)
    errors: list[dict[str, Any]] = []

    async def run_item(index: int, item: Any) -> None:
        async with semaphore:
            ref_ctx = RefContext(state=ctx.state_values, node_outputs=ctx.node_outputs,
                                 item={config.item_as: item})
            resolved = {
                k: v if k in template_type.defer_keys else resolve(v, ref_ctx)
                for k, v in template.items()
            }
            item_config = template_type.config_model.model_validate(resolved)
            item_ctx = NodeContext(
                node=ctx.node, config=item_config, state_values=ctx.state_values,
                node_outputs=ctx.node_outputs, services=ctx.services,
                thread_id=ctx.thread_id, run_id=ctx.run_id, step=ctx.step,
                item={config.item_as: item}, _emit_chunk=ctx._emit_chunk,
            )
            try:
                result = await template_type.handler(item_ctx)
                results[index] = result.output
                ok[index] = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — settled per item
                if config.on_item_error != "skip":
                    raise
                errors.append({"index": index, "error": str(exc),
                               "error_type": type(exc).__name__})

    try:
        async with asyncio.TaskGroup() as group:
            for index, item in enumerate(items):
                group.create_task(run_item(index, item))
    except BaseExceptionGroup as eg:
        # on_item_error="fail": surface the underlying error (preserving its
        # transient classification / CancelledError), not the group wrapper.
        exc: BaseException = eg
        while isinstance(exc, BaseExceptionGroup):
            exc = exc.exceptions[0]
        raise exc from None

    collected = [results[i] for i in range(len(items)) if ok[i]]
    updates = {config.collect.channel: collected} if config.collect else {}
    return NodeResult(output={"results": collected, "errors": errors,
                              "count": len(collected)},
                      updates=updates)


# ------------------------------------------------------------------- merge

class MergeConfig(BaseModel):
    wait_for: str = "all"
    success_threshold: int | None = None   # default: every branch must succeed


@node_type("merge", config_model=MergeConfig)
async def merge_node(ctx: NodeContext) -> NodeResult:
    config: MergeConfig = ctx.config  # type: ignore[assignment]
    # arrivals maps source -> "ok" | "failed" | "skipped". A skipped branch
    # (routed away by a condition) neither counts toward the threshold nor
    # against it; the default threshold is "every branch that actually ran".
    arrivals: dict[str, str] = (ctx.item or {}).get("arrivals", {})
    successes = sum(1 for st in arrivals.values() if st == "ok")
    required = sum(1 for st in arrivals.values() if st != "skipped")
    threshold = config.success_threshold if config.success_threshold is not None \
        else required
    if successes < threshold:
        raise GraphxError(
            f"merge '{ctx.node.id}': only {successes}/{required} branches succeeded "
            f"(threshold {threshold})")
    return NodeResult(output={"arrivals": arrivals, "successes": successes})
