"""Run eval cases through the real engine and derive per-case results.

Mirrors cli._run_loop — no new engine code, just a driver.
"""

from __future__ import annotations

from pathlib import Path

from ..engine.events import EventBus, RunEvent
from ..engine.executor import Executor
from ..model.graph import Graph
from ..persistence.db import open_db
from ..persistence.sqlite_checkpointer import SqliteCheckpointer
from ..runtime import graph_services
from .assertions import check_expect
from .dataset import EvalCase, EvalDataset
from .judge import judge_case
from .metrics import derive_run_report
from .report import CaseResult, EvalReport
from .store import EventStore


async def run_case(graph: Graph, case: EvalCase, db_path: str | Path,
                   dataset_name: str) -> tuple[CaseResult, list[RunEvent]]:
    thread_id = f"eval-{dataset_name}-{case.name}"
    db = await open_db(db_path)
    try:
        checkpointer = SqliteCheckpointer(db)
        events: list[RunEvent] = []
        bus = EventBus(run_id=thread_id, thread_id=thread_id,
                       sink=checkpointer.event_sink)

        async def collect() -> None:
            async for e in bus.subscribe():
                events.append(e)

        import asyncio
        task = asyncio.create_task(collect())
        error = None
        try:
            async with graph_services(graph) as services:
                outcome = await Executor(graph, checkpointer, bus, services).run(
                    thread_id, dict(case.input))
        except Exception as exc:  # noqa: BLE001 — recorded as a case error
            error = f"{type(exc).__name__}: {exc}"
            outcome = None
        finally:
            bus.close()
            await task

        store = EventStore(db)
        run_metrics = await store.run_metrics(thread_id)
        report = derive_run_report(events, run_metrics)
        node_outputs = {e.node_id: e.data.get("output")
                        for e in events if e.type.value == "node_finished" and e.node_id}
        final_state = outcome.state if outcome else {}

        checks = check_expect(case.expect, report.status,
                              report.dead_letters, final_state, node_outputs, report)
        if case.expect.judge is not None and error is None:
            checks.append(await judge_case(case.expect.judge, final_state, node_outputs,
                                           graph))
        passed = error is None and all(c.ok for c in checks)
        result = CaseResult(name=case.name, passed=passed, checks=checks,
                            metrics=report, final_state=final_state,
                            run_id=thread_id, error=error)
        return result, events
    finally:
        await db.close()


async def run_dataset(graph: Graph, dataset: EvalDataset, db_path: str | Path,
                      ) -> tuple[EvalReport, dict[str, list[RunEvent]]]:
    report = EvalReport(dataset=dataset.dataset, workflow=graph.name)
    traces: dict[str, list[RunEvent]] = {}
    for case in dataset.cases:
        result, events = await run_case(graph, case, db_path, dataset.dataset)
        report.cases.append(result)
        traces[case.name] = events
    return report, traces
