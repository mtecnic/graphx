"""HTTP API: start, watch, resume, and cancel workflow runs.

The same RunEvent stream the CLI and TUI consume, served over SSE with
Last-Event-ID replay from SQLite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..engine.events import EventBus
from ..engine.executor import Executor, RunOutcome
from ..engine.interrupts import Command
from ..model.validate import has_errors, validate_graph
from ..model.yaml_loader import LoadError, load_graph
from ..nodes.registry import known_types, load_builtin_nodes
from ..persistence.db import open_db
from ..persistence.sqlite_checkpointer import SqliteCheckpointer
from ..runtime import graph_services
from .sse import sse_response


class StartRun(BaseModel):
    workflow: str
    input: dict[str, Any] = {}
    thread_id: str | None = None


class ResumeRun(BaseModel):
    workflow: str | None = None
    answer: Any = None
    goto: str | None = None
    update: dict[str, Any] | None = None


@dataclass
class RunHandle:
    thread_id: str
    workflow_path: Path
    task: asyncio.Task | None = None
    bus: EventBus | None = None
    outcome: RunOutcome | None = None
    started_seq: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def create_app(workflow_dir: Path | str = ".", db_path: Path | str | None = None) -> FastAPI:
    workflow_dir = Path(workflow_dir)
    db_path = Path(db_path) if db_path else workflow_dir / ".graphx" / "graphx.db"
    load_builtin_nodes()

    app = FastAPI(title="graphx", version="0.4")
    runs: dict[str, RunHandle] = {}
    app.state.runs = runs

    def resolve_workflow(name: str) -> Path:
        path = Path(name)
        if not path.is_absolute():
            path = workflow_dir / path
        if not path.exists():
            raise HTTPException(404, f"workflow not found: {name}")
        return path

    def load_and_validate(path: Path):
        try:
            graph = load_graph(path)
        except LoadError as exc:
            raise HTTPException(422, f"invalid workflow: {exc}") from exc
        issues = validate_graph(graph, known_types=known_types())
        if has_errors(issues):
            raise HTTPException(422, "; ".join(
                str(i) for i in issues if i.severity == "error"))
        return graph

    async def execute(handle: RunHandle, first_input: dict[str, Any] | Command) -> None:
        graph = load_and_validate(handle.workflow_path)
        db = await open_db(db_path)
        try:
            checkpointer = SqliteCheckpointer(db)
            async with graph_services(graph) as services:
                bus = EventBus(run_id=uuid4().hex[:12], thread_id=handle.thread_id,
                               sink=checkpointer.event_sink)
                handle.bus = bus
                executor = Executor(graph, checkpointer, bus, services)
                try:
                    handle.outcome = await executor.run(handle.thread_id, first_input)
                finally:
                    bus.close()
        except Exception as exc:  # noqa: BLE001 — surfaced via status endpoint
            handle.extra["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await db.close()

    # ------------------------------------------------------------- routes

    @app.get("/workflows")
    async def list_workflows() -> list[dict[str, Any]]:
        out = []
        for path in sorted(workflow_dir.glob("*.yaml")) + sorted(workflow_dir.glob("*.yml")):
            try:
                graph = load_graph(path)
            except Exception:  # noqa: BLE001 — non-workflow yaml files are skipped
                continue
            out.append({"name": graph.name, "path": str(path),
                        "description": graph.description,
                        "nodes": len(graph.nodes)})
        return out

    @app.post("/runs", status_code=201)
    async def start_run(body: StartRun) -> dict[str, Any]:
        path = resolve_workflow(body.workflow)
        load_and_validate(path)
        thread_id = body.thread_id or uuid4().hex[:12]
        if thread_id in runs and runs[thread_id].task and not runs[thread_id].task.done():
            raise HTTPException(409, f"thread '{thread_id}' is already running")
        handle = RunHandle(thread_id=thread_id, workflow_path=path)
        handle.task = asyncio.create_task(execute(handle, dict(body.input)))
        runs[thread_id] = handle
        return {"thread_id": thread_id, "workflow": str(path)}

    @app.get("/runs/{thread_id}")
    async def run_status(thread_id: str) -> dict[str, Any]:
        handle = runs.get(thread_id)
        if handle is not None:
            running = handle.task is not None and not handle.task.done()
            outcome = handle.outcome
            return {
                "thread_id": thread_id,
                "running": running,
                "status": outcome.status if outcome else ("running" if running else "unknown"),
                "step": outcome.step if outcome else None,
                "state": outcome.state if outcome else None,
                "interrupt": ({"node_id": outcome.interrupt.node_id,
                               "payload": outcome.interrupt.payload}
                              if outcome and outcome.interrupt else None),
                "error": (outcome.error if outcome else None) or handle.extra.get("error"),
                "dead_letters": len(outcome.dead_letters) if outcome else 0,
            }
        # fall back to the latest checkpoint on disk
        db = await open_db(db_path)
        try:
            ckpt = await SqliteCheckpointer(db).load_latest(thread_id)
        finally:
            await db.close()
        if ckpt is None:
            raise HTTPException(404, f"unknown thread '{thread_id}'")
        return {
            "thread_id": thread_id, "running": False,
            "status": ("interrupted" if ckpt.pending_interrupt
                       else "finished" if not ckpt.frontier else "stopped"),
            "step": ckpt.step, "state": ckpt.state,
            "interrupt": ({"node_id": ckpt.pending_interrupt.node_id,
                           "payload": ckpt.pending_interrupt.payload}
                          if ckpt.pending_interrupt else None),
            "error": None,
            "dead_letters": len(ckpt.dead_letters),
        }

    @app.post("/runs/{thread_id}/resume")
    async def resume_run(thread_id: str, body: ResumeRun) -> dict[str, Any]:
        handle = runs.get(thread_id)
        if handle is not None and handle.task is not None and not handle.task.done():
            raise HTTPException(409, f"thread '{thread_id}' is still running")
        if handle is None:
            if not body.workflow:
                raise HTTPException(422, "server does not know this thread; "
                                         "pass 'workflow' in the body")
            handle = RunHandle(thread_id=thread_id,
                               workflow_path=resolve_workflow(body.workflow))
            runs[thread_id] = handle
        command = Command(resume=body.answer, goto=body.goto, update=body.update)
        handle.outcome = None
        handle.task = asyncio.create_task(execute(handle, command))
        return {"thread_id": thread_id, "resumed": True}

    @app.post("/runs/{thread_id}/cancel")
    async def cancel_run(thread_id: str) -> dict[str, Any]:
        handle = runs.get(thread_id)
        if handle is None or handle.task is None or handle.task.done():
            raise HTTPException(409, f"thread '{thread_id}' is not running")
        handle.task.cancel()
        try:
            await handle.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return {"thread_id": thread_id, "cancelled": True}

    @app.get("/runs/{thread_id}/events")
    async def run_events(thread_id: str, request: Request):
        last_id = request.headers.get("last-event-id")
        after_seq = int(last_id) if last_id and last_id.isdigit() else 0
        handle = runs.get(thread_id)

        db = await open_db(db_path)
        try:
            stored = await SqliteCheckpointer(db).events_for(thread_id)
        finally:
            await db.close()
        if not stored and handle is None:
            raise HTTPException(404, f"unknown thread '{thread_id}'")

        async def generate():
            counter = 0
            yielded: set[tuple[str, int]] = set()
            for event in stored:
                counter += 1
                yielded.add((event.run_id, event.seq))
                if counter > after_seq:
                    yield {"id": str(counter), "event": event.type.value,
                           "data": event.model_dump_json()}
            live = runs.get(thread_id)
            if live is not None and live.bus is not None:
                async for event in live.bus.subscribe():
                    if (event.run_id, event.seq) in yielded:
                        continue  # already replayed from the DB
                    counter += 1
                    if counter > after_seq:
                        yield {"id": str(counter), "event": event.type.value,
                               "data": event.model_dump_json()}

        return sse_response(generate())

    return app
