"""The superstep loop.

Timeline: checkpoint(step N, frontier) -> execute frontier -> merge
writes -> route -> checkpoint(step N+1, next frontier) -> ...

Because a checkpoint always precedes the superstep it describes, a
crash or cancel at ANY point resumes by re-running the in-flight
superstep — nothing before it is ever replayed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from ..model.graph import Graph
from ..model.refs import RefContext, resolve
from ..model.state import State
from ..nodes.registry import NodeContext, NodeResult, get_node_type
from .cache import input_hash
from .checkpoint import Checkpoint, Checkpointer, DeadLetter, RunMetrics, TaskSpec
from .errors import ConfigError, GuardTripped, InterruptSignal
from .events import EventBus, EventType
from .guards import Guards
from .interrupts import Command, Interrupt
from .resilience import Settled, run_with_resilience
from .scheduler import RouteOutcome, Scheduler
from .services import Services


@dataclass
class RunOutcome:
    status: Literal["finished", "interrupted", "failed", "guard_tripped", "cancelled"]
    state: dict[str, Any]
    step: int
    metrics: RunMetrics
    interrupt: Interrupt | None = None
    error: str | None = None
    dead_letters: tuple[DeadLetter, ...] = ()


@dataclass
class _TaskDone:
    task: TaskSpec
    settled: Settled | None = None
    interrupt_payload: Any = None
    config_error: str | None = None

    @property
    def interrupted(self) -> bool:
        return self.interrupt_payload is not None or isinstance(
            self.settled and self.settled.error, InterruptSignal)


def _summarize(output: Any, limit: int = 200) -> Any:
    if isinstance(output, dict):
        return {k: _summarize(v, limit) for k, v in output.items()}
    if isinstance(output, str) and len(output) > limit:
        return output[:limit] + f"… (+{len(output) - limit} chars)"
    if isinstance(output, list) and len(output) > 10:
        return [*output[:10], f"… (+{len(output) - 10} items)"]
    return output


class Executor:
    def __init__(self, graph: Graph, checkpointer: Checkpointer,
                 bus: EventBus, services: Services | None = None):
        self.graph = graph
        self.checkpointer = checkpointer
        self.bus = bus
        self.services = services or Services()
        self.scheduler = Scheduler(graph)

    # ------------------------------------------------------------------ setup

    async def run(self, thread_id: str,
                  input: dict[str, Any] | Command) -> RunOutcome:  # noqa: A002
        run_id = self.bus.run_id
        resume_node: str | None = None
        resume_value: Any = None

        if isinstance(input, Command):
            ckpt = await self.checkpointer.load_latest(thread_id)
            if ckpt is None:
                raise ConfigError(f"nothing to resume: no checkpoint for thread '{thread_id}'")
            state = State.from_json(self.graph.channels, ckpt.state)
            if input.update:
                state = state.apply(input.update)
            frontier = list(ckpt.frontier)
            node_outputs = dict(ckpt.node_outputs)
            visits = dict(ckpt.visits)
            join_arrivals = {k: dict(v) for k, v in ckpt.join_arrivals.items()}
            metrics = ckpt.metrics
            dead_letters = list(ckpt.dead_letters)
            step = ckpt.step
            if ckpt.pending_interrupt is not None:
                resume_node = ckpt.pending_interrupt.node_id
                resume_value = input.resume
            if input.goto:
                frontier = [TaskSpec(node_id=input.goto)]
            await self.bus.emit(EventType.RUN_RESUMED, step=step,
                                resume=_summarize(input.resume), goto=input.goto)
        else:
            state = State.initial(self.graph.channels, input)
            visits = {}
            frontier = []
            bootstrap = RouteOutcome()
            for entry in self.graph.entry:
                self.scheduler._schedule(entry, bootstrap, visits)
            frontier = bootstrap.frontier
            node_outputs = {}
            join_arrivals = {}
            metrics = RunMetrics()
            dead_letters = []
            step = 0
            await self.bus.emit(EventType.RUN_STARTED, step=0, graph=self.graph.name,
                                inputs=_summarize(dict(input)))

        guards = Guards(self.graph.config, self.services.clock,
                        already_elapsed_s=metrics.elapsed_s)
        semaphore = asyncio.Semaphore(self.graph.config.max_concurrency)

        await self._save(thread_id, run_id, step, state, frontier, node_outputs,
                         visits, join_arrivals, None, metrics, dead_letters)

        # ------------------------------------------------------------- loop
        try:
            while frontier:
                try:
                    guards.check(step, metrics)
                except GuardTripped as trip:
                    await self.bus.emit(EventType.GUARD_TRIPPED, step=step,
                                        guard=trip.guard, detail=trip.detail)
                    await self.bus.emit(EventType.RUN_FAILED, step=step, error=str(trip))
                    return RunOutcome("guard_tripped", state.to_json(), step, metrics,
                                      error=str(trip), dead_letters=tuple(dead_letters))

                await self.bus.emit(EventType.SUPERSTEP_STARTED, step=step,
                                    frontier=[t.node_id for t in frontier])

                done: list[_TaskDone] = []
                async with asyncio.TaskGroup() as group:
                    for task in frontier:
                        rv = resume_value if task.node_id == resume_node else None
                        group.create_task(self._run_task(
                            task, state, node_outputs, visits, step, semaphore,
                            done, resume_val=rv))
                resume_node = resume_value = None
                # frontier order is deterministic; restore it after concurrent completion
                order = {t.task_id: i for i, t in enumerate(frontier)}
                done.sort(key=lambda d: order[d.task.task_id])

                # ---- merge state writes (deterministic order) ----
                interrupted: list[_TaskDone] = []
                changed: dict[str, Any] = {}
                for item in done:
                    if item.interrupted:
                        interrupted.append(item)
                        continue
                    settled = item.settled
                    if settled is None or not settled.ok or settled.result is None:
                        continue
                    result = settled.result
                    node = self.graph.nodes[item.task.node_id]
                    node_outputs[node.id] = result.output
                    updates = dict(result.updates)
                    if item.task.collect_channel:
                        updates[item.task.collect_channel] = result.output
                    if node.updates:
                        ref_ctx = RefContext(state=state.values, node_outputs=node_outputs,
                                             item=item.task.item, self_output=result.output)
                        updates.update(resolve(dict(node.updates), ref_ctx))
                    if updates:
                        state = state.apply(updates)
                        changed.update({k: _summarize(state.get(k)) for k in updates})
                    metrics.tokens += result.metrics.tokens
                    metrics.cost_usd += result.metrics.cost_usd
                    metrics.nodes_run += 1
                if changed:
                    await self.bus.emit(EventType.STATE_UPDATED, step=step, changed=changed)

                # ---- routing ----
                outcome = RouteOutcome()
                for item in done:
                    if item.interrupted:
                        continue
                    settled = item.settled
                    if settled is not None and settled.ok and settled.result is not None:
                        self.scheduler.route_success(
                            item.task.node_id, settled.result.goto, settled.result.sends,
                            dict(state.values), node_outputs, visits, join_arrivals, outcome)
                    else:
                        error = item.config_error or (
                            str(settled.error) if settled and settled.error else "unknown error")
                        error_type = type(settled.error).__name__ if settled and settled.error \
                            else "ConfigError"
                        dead_letters.append(DeadLetter(
                            node_id=item.task.node_id,
                            attempts=settled.attempts if settled else 0,
                            error=error, error_type=error_type, step=step))
                        self.scheduler.route_failure(item.task.node_id, visits,
                                                     join_arrivals, outcome)

                metrics.elapsed_s = guards.elapsed_s()

                # ---- interrupt: park the thread ----
                if interrupted:
                    first = interrupted[0]
                    interrupt = Interrupt(node_id=first.task.node_id,
                                          payload=first.interrupt_payload)
                    next_frontier = [i.task for i in interrupted] + outcome.frontier
                    await self._save(thread_id, run_id, step, state, next_frontier,
                                     node_outputs, visits, join_arrivals, interrupt,
                                     metrics, dead_letters)
                    await self.bus.emit(EventType.INTERRUPT_RAISED, step=step,
                                        node_id=interrupt.node_id,
                                        interrupt_id=interrupt.interrupt_id,
                                        payload=_summarize(interrupt.payload))
                    await self.bus.emit(EventType.RUN_INTERRUPTED, step=step,
                                        node_id=interrupt.node_id)
                    return RunOutcome("interrupted", state.to_json(), step, metrics,
                                      interrupt=interrupt, dead_letters=tuple(dead_letters))

                if outcome.fatal_failures:
                    await self._save(thread_id, run_id, step + 1, state, [], node_outputs,
                                     visits, join_arrivals, None, metrics, dead_letters)
                    error = f"node(s) failed with no error route: {', '.join(outcome.fatal_failures)}"
                    await self.bus.emit(EventType.RUN_FAILED, step=step, error=error,
                                        dead_letters=len(dead_letters))
                    return RunOutcome("failed", state.to_json(), step, metrics, error=error,
                                      dead_letters=tuple(dead_letters))

                for warn in guards.warnings(step + 1, metrics):
                    await self.bus.emit(EventType.BUDGET_WARNING, step=step, guard=warn.guard,
                                        used=warn.used, limit=warn.limit)

                step += 1
                frontier = outcome.frontier
                await self._save(thread_id, run_id, step, state, frontier, node_outputs,
                                 visits, join_arrivals, None, metrics, dead_letters)
                await self.bus.emit(EventType.CHECKPOINT_SAVED, step=step)
                await self.bus.emit(EventType.SUPERSTEP_FINISHED, step=step - 1,
                                    next=[t.node_id for t in frontier])

        except asyncio.CancelledError:
            # last checkpoint already covers the in-flight superstep
            await self.bus.emit(EventType.RUN_CANCELLED, step=step)
            raise

        await self.bus.emit(EventType.RUN_FINISHED, step=step,
                            state=_summarize(state.to_json()),
                            dead_letters=len(dead_letters))
        return RunOutcome("finished", state.to_json(), step, metrics,
                          dead_letters=tuple(dead_letters))

    # ------------------------------------------------------------- one task

    async def _run_task(self, task: TaskSpec, state: State,
                        node_outputs: dict[str, Any], visits: dict[str, int],
                        step: int, semaphore: asyncio.Semaphore,
                        done: list[_TaskDone], resume_val: Any) -> None:
        async with semaphore:
            node = self.graph.nodes[task.node_id]
            await self.bus.emit(EventType.NODE_STARTED, step=step, node_id=node.id,
                                attempt=1, visit=visits.get(node.id, 1))
            try:
                node_type = get_node_type(node.type)
                ref_ctx = RefContext(state=state.values, node_outputs=node_outputs,
                                     item=task.item)
                resolved = {
                    key: value if key in node_type.defer_keys else resolve(value, ref_ctx)
                    for key, value in dict(node.config).items()
                }
                config = node_type.config_model.model_validate(resolved)
            except (ValidationError, KeyError, ValueError) as exc:
                message = f"config invalid: {exc}"
                done.append(_TaskDone(task, config_error=message))
                await self.bus.emit(EventType.NODE_FAILED, step=step, node_id=node.id,
                                    error=message, error_type="ConfigError")
                return

            policy = node.resilience
            if policy.cache:
                digest = input_hash(node.type, resolved, task.item)
                cached = await self.checkpointer.cache_get(node.id, digest)
                if cached is not None:
                    result = NodeResult(output=cached.get("output"),
                                        updates=cached.get("updates", {}))
                    result.metrics.cached = True
                    done.append(_TaskDone(task, settled=Settled(ok=True, result=result)))
                    await self.bus.emit(EventType.NODE_CACHED, step=step, node_id=node.id,
                                        input_hash=digest[:16])
                    await self.bus.emit(EventType.NODE_FINISHED, step=step, node_id=node.id,
                                        output=_summarize(result.output), cached=True)
                    return

            async def emit_chunk(chunk: str) -> None:
                await self.bus.emit(EventType.NODE_OUTPUT_CHUNK, step=step,
                                    node_id=node.id, chunk=chunk)

            source_dir = Path(self.graph.source_path).parent \
                if self.graph.source_path else None
            ctx = NodeContext(
                node=node, config=config, state_values=state.values,
                node_outputs=node_outputs, services=self.services,
                thread_id=self.bus.thread_id, run_id=self.bus.run_id, step=step,
                visit=visits.get(node.id, 1), item=task.item,
                resume_value=resume_val, source_dir=source_dir,
                _emit_chunk=emit_chunk,
            )
            try:
                settled = await run_with_resilience(
                    node_type.handler, ctx, policy, self.bus, self.services)
            except InterruptSignal as signal:
                done.append(_TaskDone(task, interrupt_payload=signal.payload))
                return

            if settled.ok and settled.result is not None:
                if policy.cache:
                    await self.checkpointer.cache_put(node.id, digest, {
                        "output": settled.result.output,
                        "updates": dict(settled.result.updates),
                    })
                await self.bus.emit(
                    EventType.NODE_FINISHED, step=step, node_id=node.id,
                    output=_summarize(settled.result.output),
                    duration_s=round(settled.duration_s, 3),
                    attempts=settled.attempts,
                    degraded=settled.result.metrics.degraded or None)
            else:
                await self.bus.emit(
                    EventType.NODE_FAILED, step=step, node_id=node.id,
                    error=str(settled.error), error_type=type(settled.error).__name__,
                    attempts=settled.attempts)
            done.append(_TaskDone(task, settled=settled))

    # ---------------------------------------------------------- checkpoint

    async def _save(self, thread_id: str, run_id: str, step: int, state: State,
                    frontier: list[TaskSpec], node_outputs: dict[str, Any],
                    visits: dict[str, int], join_arrivals: dict[str, dict[str, bool]],
                    interrupt: Interrupt | None, metrics: RunMetrics,
                    dead_letters: list[DeadLetter]) -> None:
        await self.checkpointer.save(Checkpoint(
            thread_id=thread_id, run_id=run_id, step=step,
            graph_name=self.graph.name, state=state.to_json(),
            frontier=tuple(frontier), node_outputs=dict(node_outputs),
            visits=dict(visits), join_arrivals={k: dict(v) for k, v in join_arrivals.items()},
            pending_interrupt=interrupt, metrics=metrics,
            dead_letters=tuple(dead_letters),
        ))


def new_run_bus(thread_id: str, sink=None) -> EventBus:
    return EventBus(run_id=uuid4().hex[:12], thread_id=thread_id, sink=sink)
