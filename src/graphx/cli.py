"""graphx CLI: validate / run / resume / events / tui."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from rich.console import Console

from .engine.events import EventBus, EventType, RunEvent
from .engine.executor import Executor, RunOutcome
from .engine.interrupts import Command
from .model.validate import has_errors, validate_graph
from .model.yaml_loader import load_graph
from .nodes.registry import known_types, load_builtin_nodes
from .persistence.db import default_db_path, open_db
from .persistence.sqlite_checkpointer import SqliteCheckpointer

app = typer.Typer(help="TUI-native designer and runner for agentic workflows.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console(stderr=False)

_STATUS_STYLE = {
    EventType.NODE_STARTED: ("cyan", "▶"),
    EventType.NODE_FINISHED: ("green", "✔"),
    EventType.NODE_FAILED: ("red", "✘"),
    EventType.NODE_RETRYING: ("yellow", "↻"),
    EventType.NODE_FALLBACK: ("yellow", "⇄"),
    EventType.NODE_CACHED: ("blue", "≡"),
    EventType.INTERRUPT_RAISED: ("magenta", "⏸"),
    EventType.GUARD_TRIPPED: ("red", "⛔"),
    EventType.BUDGET_WARNING: ("yellow", "⚠"),
    EventType.RUN_FINISHED: ("green", "■"),
    EventType.RUN_FAILED: ("red", "■"),
    EventType.RUN_INTERRUPTED: ("magenta", "■"),
}


def _print_event(event: RunEvent, verbose: bool) -> None:
    style, icon = _STATUS_STYLE.get(event.type, ("dim", "·"))
    if not verbose and event.type in (
        EventType.CHECKPOINT_SAVED, EventType.SUPERSTEP_STARTED,
        EventType.SUPERSTEP_FINISHED, EventType.STATE_UPDATED,
    ):
        return
    if event.type == EventType.NODE_OUTPUT_CHUNK:
        console.print(f"[dim]  {event.node_id} |[/dim] {event.data.get('chunk', '').rstrip()}",
                      highlight=False)
        return
    node = f" [bold]{event.node_id}[/bold]" if event.node_id else ""
    detail = ""
    if event.data:
        keys = {k: v for k, v in event.data.items() if k not in ("frontier", "next")}
        if keys:
            detail = "  [dim]" + json.dumps(keys, default=str)[:240] + "[/dim]"
    console.print(f"[{style}]{icon}[/{style}] [dim]s{event.step}[/dim]"
                  f"{node} {event.type.value}{detail}", highlight=False)


def _parse_kv(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--input expects key=value, got {pair!r}")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def _parse_answer(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _load(workflow: Path):
    load_builtin_nodes()
    graph = load_graph(workflow)
    issues = validate_graph(graph, known_types=known_types())
    for issue in issues:
        style = "red" if issue.severity == "error" else "yellow"
        console.print(f"[{style}]{issue}[/{style}]")
    if has_errors(issues):
        raise typer.Exit(code=1)
    return graph


@app.command()
def validate(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Statically check a workflow file."""
    graph = _load(workflow)
    console.print(f"[green]✔[/green] {graph.name}: {len(graph.nodes)} nodes, "
                  f"{len(graph.edges)} edges, entry={list(graph.entry)}")


async def _run_loop(graph, thread_id: str, first_input: dict[str, Any] | Command,
                    db_path: Path, verbose: bool, interactive: bool) -> RunOutcome:
    from .runtime import graph_services

    db = await open_db(db_path)
    try:
        async with graph_services(graph) as services:
            checkpointer = SqliteCheckpointer(db)
            next_input: dict[str, Any] | Command = first_input
            while True:
                bus = EventBus(run_id=uuid4().hex[:12], thread_id=thread_id,
                               sink=checkpointer.event_sink)

                async def print_events() -> None:
                    async for event in bus.subscribe():
                        _print_event(event, verbose)

                printer = asyncio.create_task(print_events())
                executor = Executor(graph, checkpointer, bus, services)
                try:
                    outcome = await executor.run(thread_id, next_input)
                finally:
                    bus.close()
                    await printer

                if (outcome.status != "interrupted" or not interactive
                        or not sys.stdin.isatty()):
                    return outcome

                payload = outcome.interrupt.payload if outcome.interrupt else {}
                prompt = payload.get("prompt", "input required")
                choices = payload.get("choices")
                if payload.get("payload") is not None:
                    console.print_json(json.dumps(payload["payload"], default=str))
                suffix = f" [{'/'.join(choices)}]" if choices else ""
                answer = console.input(f"[magenta]⏸ {prompt}{suffix}: [/magenta]").strip()
                next_input = Command(resume=_parse_answer(answer))
    finally:
        await db.close()


def _finish(outcome: RunOutcome, thread_id: str) -> None:
    console.print(f"\n[bold]status:[/bold] {outcome.status}  "
                  f"[bold]steps:[/bold] {outcome.step}  "
                  f"[bold]thread:[/bold] {thread_id}")
    if outcome.status == "interrupted" and outcome.interrupt:
        payload = outcome.interrupt.payload or {}
        console.print(f"[magenta]waiting at '{outcome.interrupt.node_id}': "
                      f"{payload.get('prompt', '')}[/magenta]")
        console.print(f"resume with: [bold]graphx resume <workflow> {thread_id} "
                      f"--answer <value>[/bold]")
    if outcome.dead_letters:
        console.print(f"[red]{len(outcome.dead_letters)} dead-letter(s):[/red]")
        for dl in outcome.dead_letters:
            console.print(f"  [red]•[/red] {dl.node_id} (step {dl.step}, "
                          f"{dl.attempts} attempts): {dl.error_type}: {dl.error[:200]}")
    console.print("[bold]final state:[/bold]")
    console.print_json(json.dumps(outcome.state, default=str))
    if outcome.status in ("failed", "guard_tripped"):
        raise typer.Exit(code=1)


@app.command()
def run(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        input: Annotated[list[str], typer.Option("--input", "-i", help="key=value")] = [],
        thread: Annotated[str | None, typer.Option(help="thread id (resumable)")] = None,
        db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
        interactive: Annotated[bool, typer.Option(help="answer human gates inline")] = True,
        ) -> None:
    """Run a workflow, streaming events."""
    graph = _load(workflow)
    thread_id = thread or uuid4().hex[:12]
    outcome = asyncio.run(_run_loop(graph, thread_id, _parse_kv(input),
                                    db or default_db_path(), verbose, interactive))
    _finish(outcome, thread_id)


@app.command()
def resume(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
           thread: Annotated[str, typer.Argument(help="thread id to resume")],
           answer: Annotated[str | None, typer.Option(help="answer for a pending gate")] = None,
           goto: Annotated[str | None, typer.Option(help="override next node")] = None,
           db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
           verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
           ) -> None:
    """Resume an interrupted or crashed thread from its last checkpoint."""
    graph = _load(workflow)
    command = Command(resume=_parse_answer(answer) if answer is not None else None, goto=goto)
    outcome = asyncio.run(_run_loop(graph, thread, command,
                                    db or default_db_path(), verbose, True))
    _finish(outcome, thread)


@app.command()
def events(run_or_thread: Annotated[str, typer.Argument(help="run id or thread id")],
           db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
           as_json: Annotated[bool, typer.Option("--json", help="JSONL output "
                                                 "(replayable with graphx tui --trace)")] = False,
           ) -> None:
    """Print the stored event log of a past run."""
    async def fetch() -> list[RunEvent]:
        conn = await open_db(db or default_db_path())
        try:
            return await SqliteCheckpointer(conn).events_for(run_or_thread)
        finally:
            await conn.close()

    stored = asyncio.run(fetch())
    if not stored:
        console.print("[yellow]no events found[/yellow]")
        raise typer.Exit(code=1)
    for event in stored:
        if as_json:
            print(event.model_dump_json())
        else:
            _print_event(event, verbose=True)


@app.command()
def history(thread: Annotated[str, typer.Argument(help="thread id")],
            db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
            ) -> None:
    """List the checkpoints of a thread (step, run, saved_at)."""
    async def fetch():
        conn = await open_db(db or default_db_path())
        try:
            checkpointer = SqliteCheckpointer(conn)
            metas = await checkpointer.history(thread)
            latest = await checkpointer.load_latest(thread)
            return metas, latest
        finally:
            await conn.close()

    metas, latest = asyncio.run(fetch())
    if not metas:
        console.print("[yellow]no checkpoints for that thread[/yellow]")
        raise typer.Exit(code=1)
    for meta in metas:
        console.print(f"  step {meta.step:>3}  run {meta.run_id}  [dim]{meta.saved_at}[/dim]")
    if latest:
        frontier = [t.node_id for t in latest.frontier]
        status = "waiting on " + latest.pending_interrupt.node_id \
            if latest.pending_interrupt else ("pending: " + ", ".join(frontier)
                                              if frontier else "finished")
        console.print(f"[bold]latest:[/bold] step {latest.step} — {status}")
        console.print_json(json.dumps(latest.state, default=str))


@app.command()
def tui(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        trace: Annotated[Path | None, typer.Option(help="play a recorded event trace")] = None,
        db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
        attach: Annotated[str | None, typer.Option(help="graphx server URL to attach to, "
                                                   "e.g. http://localhost:8420")] = None,
        thread: Annotated[str | None, typer.Option(help="thread id to follow "
                                                   "(with --attach)")] = None,
        ) -> None:
    """Open the TUI: graph view + live runs (local or attached to a server)."""
    graph = _load(workflow)
    from .tui.app import GraphxApp
    GraphxApp(graph, workflow_path=workflow, trace_path=trace,
              db_path=db or default_db_path(),
              attach_url=attach, attach_thread=thread).run()


@app.command()
def serve(dir: Annotated[Path, typer.Argument(help="directory of workflow yamls")] = Path("."),
          host: Annotated[str, typer.Option()] = "127.0.0.1",
          port: Annotated[int, typer.Option()] = 8420,
          db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
          ) -> None:
    """Serve the HTTP API (REST + SSE event streams)."""
    import uvicorn

    from .server.app import create_app
    console.print(f"[bold]graphx API[/bold] http://{host}:{port} — workflows from {dir}")
    uvicorn.run(create_app(dir, db), host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
