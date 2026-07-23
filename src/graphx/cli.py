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

secret_app = typer.Typer(help="Manage stored credentials (referenced as secret://NAME).",
                         no_args_is_help=True)
app.add_typer(secret_app, name="secret")


@secret_app.command("set")
def secret_set(name: Annotated[str, typer.Argument(help="secret name")],
               value: Annotated[str | None, typer.Option(help="value (omit for a hidden "
                                                         "prompt; use --stdin to pipe)")] = None,
               stdin: Annotated[bool, typer.Option("--stdin", help="read value from stdin")] = False,
               ) -> None:
    """Store a secret (value never echoed)."""
    from .secrets import SecretStore
    if stdin:
        value = sys.stdin.readline().rstrip("\n")
    elif value is None:
        value = typer.prompt(f"value for {name}", hide_input=True)
    if not value:
        console.print("[red]empty value[/red]")
        raise typer.Exit(code=1)
    store = SecretStore()
    try:
        store.set(name, value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]✔[/green] stored [bold]{name}[/bold] "
                  f"[dim]({store.backend()} backend)[/dim]")


@secret_app.command("list")
def secret_list() -> None:
    """List stored secret NAMES (never values)."""
    from .secrets import SecretStore
    store = SecretStore()
    names = store.names()
    console.print(f"[dim]backend: {store.backend()}[/dim]")
    if not names:
        hint = "" if store.backend() == "file" else " (keyring can't enumerate names)"
        console.print(f"[yellow]no stored secrets{hint}[/yellow]")
        return
    for name in names:
        console.print(f"  [bold]{name}[/bold]  [dim]secret://{name}[/dim]")


@secret_app.command("rm")
def secret_rm(name: Annotated[str, typer.Argument(help="secret name")]) -> None:
    """Delete a stored secret."""
    from .secrets import SecretStore
    if SecretStore().delete(name):
        console.print(f"[green]✔[/green] removed [bold]{name}[/bold]")
    else:
        console.print(f"[yellow]no stored secret '{name}'[/yellow]")
        raise typer.Exit(code=1)

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


def _graph_secret_refs(graph) -> set[str]:
    from .secrets import find_secret_refs
    names: set[str] = set()
    for node in graph.nodes.values():
        names |= find_secret_refs(dict(node.config))
    names |= find_secret_refs(dict(graph.providers))
    names |= find_secret_refs(dict(graph.mcp_servers))
    return names


def _check_secrets(graph) -> None:
    """Fail fast if a workflow references secrets that aren't set anywhere."""
    from .secrets import SecretResolver
    missing = SecretResolver().missing(_graph_secret_refs(graph))
    if missing:
        console.print("[red]missing secret(s):[/red] " + ", ".join(missing))
        for name in missing:
            console.print(f"  set it with: [bold]graphx secret set {name}[/bold]")
        raise typer.Exit(code=1)


@app.command()
def validate(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Statically check a workflow file."""
    graph = _load(workflow)
    console.print(f"[green]✔[/green] {graph.name}: {len(graph.nodes)} nodes, "
                  f"{len(graph.edges)} edges, entry={list(graph.entry)}")
    refs = _graph_secret_refs(graph)
    if refs:
        from .secrets import SecretResolver
        missing = set(SecretResolver().missing(refs))
        for name in sorted(refs):
            mark = "[red]unset[/red]" if name in missing else "[green]set[/green]"
            console.print(f"  secret://{name}  {mark}")


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
    _check_secrets(graph)
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


def _endpoints_quick() -> list:
    """Cached endpoints, else a fast localhost-only probe (never LAN here)."""
    from .llm.discovery import load_cache, scan
    cached = load_cache()
    if cached:
        return cached
    return asyncio.run(scan(lan=False, timeout=5))


@app.command()
def new(name: Annotated[str, typer.Argument(help="workflow name → NAME.yaml")],
        template: Annotated[str, typer.Option("--template", "-t",
                                              help="blank | agent | approval | pipeline")] = "blank",
        tui_open: Annotated[bool, typer.Option("--tui", help="open the TUI on it")] = False,
        force: Annotated[bool, typer.Option(help="overwrite an existing file")] = False,
        ) -> None:
    """Create a new workflow from a template (agent template auto-fills
    a discovered local/LAN inference server)."""
    from .templates import TEMPLATES, create_workflow

    if template not in TEMPLATES:
        console.print(f"[red]unknown template '{template}'[/red] — available:")
        for t in TEMPLATES.values():
            console.print(f"  [bold]{t.key:9}[/bold] {t.description}")
        raise typer.Exit(code=1)

    endpoints = _endpoints_quick() if TEMPLATES[template].needs_llm else []
    try:
        path = create_workflow(name, template, endpoints=endpoints, force=force)
    except (ValueError, FileExistsError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]✔[/green] created [bold]{path}[/bold] "
                  f"(template: {template})")
    if TEMPLATES[template].needs_llm:
        if endpoints:
            from .llm.discovery import best_endpoint
            chosen = best_endpoint(endpoints)
            console.print(f"  model wired to discovered endpoint: "
                          f"[bold]{chosen.label}[/bold]")
        else:
            console.print("  [yellow]no inference server found — edit the "
                          "providers/model lines[/yellow]")
    if tui_open:
        tui(workflow=path, trace=None, db=None, attach=None, thread=None)
    else:
        console.print(f"  next: [bold]graphx tui {path}[/bold]")


@app.command()
def connectors(category: Annotated[str | None, typer.Option("--category", "-c",
                                                            help="filter by category")] = None,
               ) -> None:
    """List available service connectors (credential-wired node presets)."""
    from .connectors.registry import all_connectors, categories
    items = [c for c in all_connectors() if category is None or c.category == category]
    if not items:
        console.print(f"[yellow]no connectors in category '{category}'[/yellow] "
                      f"(have: {', '.join(categories())})")
        raise typer.Exit(code=1)
    current = None
    for c in items:
        if c.category != current:
            current = c.category
            console.print(f"\n[bold]{current}[/bold]")
        extra = f" [dim](needs \\[{c.extra}] extra)[/dim]" if c.extra else ""
        console.print(f"  [bold]{c.key:16}[/bold] {c.description}{extra}")
        fields = ", ".join(f.name for f in c.fields if f.required and f.default is None)
        if fields:
            console.print(f"  {'':16} [dim]fields: {fields}[/dim]")
        if c.secrets:
            console.print(f"  {'':16} [dim]secrets: "
                          f"{', '.join(s.name for s in c.secrets)}[/dim]")
    console.print("\nadd one with: [bold]graphx add <connector> <workflow> "
                  "field=value ...[/bold]")


@app.command()
def add(connector: Annotated[str, typer.Argument(help="connector key")],
        workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
        fields: Annotated[list[str], typer.Argument(help="field=value pairs")] = [],
        node_id: Annotated[str | None, typer.Option("--id", help="node id")] = None,
        after: Annotated[str | None, typer.Option(help="insert after this node")] = None,
        ) -> None:
    """Add a service connector node to a workflow."""
    from .connectors.registry import all_connectors, get
    from .model.yaml_writer import WorkflowFile

    try:
        conn = get(connector)
    except KeyError:
        console.print(f"[red]unknown connector '{connector}'[/red] — available:")
        for c in all_connectors():
            console.print(f"  [bold]{c.key}[/bold] [dim]({c.category})[/dim] {c.description}")
        raise typer.Exit(code=1) from None

    values = _parse_kv(fields)
    missing = conn.missing_fields(values)
    if missing:
        console.print(f"[red]missing required field(s):[/red] {', '.join(missing)}")
        console.print(f"  fields: {', '.join(f.name + '=' for f in conn.fields)}")
        raise typer.Exit(code=1)

    result = conn.render(node_id or f"{conn.key}", values)
    wf = WorkflowFile(workflow)
    try:
        wf.add_node(result.node, after=after)
        if result.mcp_servers:
            for name, cfg in result.mcp_servers.items():
                wf.set_mcp_server(name, cfg)
    except Exception as exc:  # noqa: BLE001 — surfaced to the user
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    # validate the resulting file before saving over it
    load_builtin_nodes()
    graph = load_graph_from_text(wf.dumps(), workflow)
    issues = validate_graph(graph, known_types=known_types())
    if has_errors(issues):
        console.print("[red]connector produced an invalid workflow:[/red]")
        for issue in issues:
            if issue.severity == "error":
                console.print(f"  {issue}")
        raise typer.Exit(code=1)
    wf.save()

    console.print(f"[green]✔[/green] added [bold]{result.node['id']}[/bold] "
                  f"({conn.title}) to {workflow}")
    if result.note:
        console.print(f"  [yellow]{result.note}[/yellow]")
    if conn.extra:
        console.print(f"  [dim]run needs: pip install 'graphx\\[{conn.extra}]'[/dim]")
    unset = _missing_secret_names(result.secret_names)
    if unset:
        console.print("  [yellow]set its credential(s):[/yellow]")
        for name in unset:
            hint = next((s.how for s in conn.secrets if s.name == name), "")
            console.print(f"    graphx secret set {name}   [dim]# {hint}[/dim]")
    console.print("  [dim]wire it with edges (or press c in the TUI)[/dim]")


def _missing_secret_names(names: list[str]) -> list[str]:
    from .secrets import SecretResolver
    return SecretResolver().missing(names)


def _print_build_result(result) -> None:
    console.print(f"[dim]engine: {result.engine}  model: {result.model}  "
                  f"~{result.tokens} tokens[/dim]")
    if result.ok:
        console.print("[green]✔ valid workflow generated[/green]")
    else:
        console.print("[yellow]⚠ could not produce a fully valid workflow "
                      "(saving as a draft to fix by hand)[/yellow]")
        for issue in result.errors:
            console.print(f"  [red]{issue}[/red]")


@app.command()
def generate(description: Annotated[str, typer.Argument(help="what the workflow should do")],
             name: Annotated[str | None, typer.Option(help="workflow name")] = None,
             out: Annotated[Path | None, typer.Option("--out", help="output .yaml path")] = None,
             engine: Annotated[str, typer.Option(help="oneshot | agentic | auto")] = "oneshot",
             agentic: Annotated[bool, typer.Option("--agentic", help="use the agentic engine")] = False,
             model: Annotated[str | None, typer.Option(help="provider/model override")] = None,
             yes: Annotated[bool, typer.Option("--yes", "-y", help="write without confirming")] = False,
             ) -> None:
    """Generate a new workflow from a natural-language description (uses your local model)."""
    from .builder import build_workflow

    load_builtin_nodes()
    wf_name = name or _slugify(description)
    endpoints = _endpoints_quick()
    with console.status("thinking…"):
        result = asyncio.run(build_workflow(
            description=description, name=wf_name, endpoints=endpoints, model=model,
            engine="agentic" if agentic else engine))
    console.print(result.yaml)
    _print_build_result(result)

    target = out or Path(f"{wf_name}.yaml")
    if not result.ok:
        target = target.with_suffix(".draft.yaml")
    if not yes and sys.stdin.isatty():
        if not typer.confirm(f"write to {target}?", default=result.ok):
            raise typer.Exit(code=0)
    target.write_text(result.yaml)
    console.print(f"[green]✔[/green] wrote {target}")
    if result.ok:
        _report_missing_secrets(result)
        console.print(f"  run it: [bold]graphx run {target}[/bold]  "
                      f"or edit: [bold]graphx tui {target}[/bold]")


@app.command()
def edit(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
         instruction: Annotated[str, typer.Argument(help="the change to make, in plain English")],
         agentic: Annotated[bool, typer.Option("--agentic", help="surgical tool-driven edit")] = False,
         model: Annotated[str | None, typer.Option(help="provider/model override")] = None,
         yes: Annotated[bool, typer.Option("--yes", "-y", help="apply without confirming")] = False,
         ) -> None:
    """Apply a natural-language edit to an existing workflow."""
    import difflib

    from .builder import build_workflow

    load_builtin_nodes()
    before = workflow.read_text()
    endpoints = _endpoints_quick()
    with console.status("thinking…"):
        result = asyncio.run(build_workflow(
            instruction=instruction, current_path=workflow, endpoints=endpoints,
            model=model, engine="agentic" if agentic else "oneshot"))

    diff = list(difflib.unified_diff(before.splitlines(), result.yaml.splitlines(),
                                     fromfile=str(workflow), tofile="proposed", lineterm=""))
    for line in diff:
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        console.print(f"[{style}]{line}[/{style}]", highlight=False)
    _print_build_result(result)
    if not result.ok:
        console.print("[yellow]not applied — the edit didn't validate[/yellow]")
        raise typer.Exit(code=1)
    if not yes and sys.stdin.isatty():
        if not typer.confirm("apply this change?", default=True):
            raise typer.Exit(code=0)
    workflow.write_text(result.yaml)
    console.print(f"[green]✔[/green] updated {workflow}")
    _report_missing_secrets(result)


def _report_missing_secrets(result) -> None:
    if result.draft is None:
        return
    unset = _missing_secret_names(sorted(result.draft.secret_refs()))
    for name in unset:
        console.print(f"  [yellow]set its credential:[/yellow] graphx secret set {name}")


def _slugify(text: str) -> str:
    import re
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    return "_".join(words) or "workflow"


def load_graph_from_text(text: str, path: Path):
    from ruamel.yaml import YAML

    from .model.yaml_loader import build_graph
    data = YAML(typ="safe", pure=True).load(text)
    return build_graph(data, source_path=str(path))


@app.command()
def providers(scan_now: Annotated[bool, typer.Option("--scan", help="force a fresh scan")] = False,
              lan: Annotated[bool, typer.Option(help="include the local /24 in the scan")] = True,
              add: Annotated[str | None, typer.Option("--add", help="probe & add an endpoint "
                                                      "by URL (e.g. http://host:8000/v1)")] = None,
              ) -> None:
    """List discovered LLM inference endpoints (Ollama/vLLM/llama.cpp/LM Studio/...)."""
    import time as _time

    from .llm.discovery import add_endpoint, discover, load_cache

    if add:
        with console.status(f"probing {add}…"):
            endpoint = asyncio.run(add_endpoint(add))
        if endpoint is None:
            console.print(f"[red]no LLM server at {add}[/red] "
                          "(expected an OpenAI-compatible /v1/models or Ollama /api/tags)")
            raise typer.Exit(code=1)
        console.print(f"[green]✔[/green] added [bold]{endpoint.alias}[/bold]  "
                      f"{endpoint.base_url}  [dim]{endpoint.kind}, "
                      f"{endpoint.response_ms}ms[/dim]")
        for model in endpoint.models:
            console.print(f"    {endpoint.alias}/{model}")
        return

    if scan_now:
        with console.status("scanning localhost + LAN…" if lan else "scanning localhost…"):
            endpoints = asyncio.run(discover(refresh=True, lan=lan,
                                             progress=lambda label: console.print(f"  • {label}")))
    else:
        endpoints = load_cache()
        if not endpoints:
            console.print("[yellow]no cached endpoints — scanning…[/yellow]")
            endpoints = asyncio.run(discover(lan=lan))

    if not endpoints:
        console.print("[red]no inference endpoints found[/red] "
                      "(ports probed: 11434, 1234, 5000, 8000, 8080)")
        raise typer.Exit(code=1)
    for endpoint in endpoints:
        age = int(_time.time() - endpoint.checked_at)
        console.print(f"[green]●[/green] [bold]{endpoint.alias}[/bold]  "
                      f"{endpoint.base_url}  [dim]{endpoint.kind}, "
                      f"{endpoint.response_ms}ms, checked {age}s ago[/dim]")
        for model in endpoint.models:
            console.print(f"    {endpoint.alias}/{model}")


@app.command("scaffold-api")
def scaffold_api(workflow: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
                 url: Annotated[str, typer.Argument(help="service base URL or spec URL")],
                 op: Annotated[str | None, typer.Option(
                     help='operation to add, e.g. "GET /runs/{thread_id}" or an '
                          "operationId; omit to list operations")] = None,
                 node_id: Annotated[str | None, typer.Option("--id")] = None,
                 after: Annotated[str | None, typer.Option(
                     help="insert after this node id")] = None,
                 ) -> None:
    """Add an api node scaffolded from a service's OpenAPI spec."""
    from .model.openapi import fetch_spec, parse_operations, scaffold_api_node
    from .model.yaml_writer import WorkflowFile

    async def fetch():
        return await fetch_spec(url if url.startswith(("http://", "https://"))
                                else "http://" + url)

    spec, base_url = asyncio.run(fetch())
    operations = parse_operations(spec)
    if op is None:
        title = (spec.get("info") or {}).get("title", "spec")
        console.print(f"[bold]{title}[/bold] — {len(operations)} operations:")
        for operation in operations:
            console.print(f"  {operation.label}")
        console.print("\nre-run with --op \"METHOD /path\" to add one")
        return

    wanted = op.strip().lower()
    match = next((o for o in operations
                  if f"{o.method} {o.path}".lower() == wanted
                  or o.operation_id.lower() == wanted), None)
    if match is None:
        console.print(f"[red]no operation matching {op!r}[/red]")
        raise typer.Exit(code=1)

    node = scaffold_api_node(match, base_url, spec, node_id)
    wf = WorkflowFile(workflow)
    wf.add_node(node, after=after)
    wf.save()
    console.print(f"[green]✔[/green] added api node "
                  f"[bold]{node['id']}[/bold] to {workflow}:")
    console.print_json(json.dumps(node, default=str))
    console.print("[dim]review the <state.…> placeholders, then wire it with "
                  "edges (or press c in the TUI)[/dim]")


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


def _default_workflow() -> Path:
    """Bare `graphx tui`: prefer a workflow in cwd, else the bundled demo."""
    candidates = sorted(Path(".").glob("*.yaml")) + sorted(Path(".").glob("*.yml"))
    valid: list[Path] = []
    for candidate in candidates:
        try:
            from .model.yaml_loader import load_raw
            if load_raw(candidate).get("version") == 1:
                valid.append(candidate)
        except Exception:  # noqa: BLE001 — not a workflow file
            continue
    if len(valid) == 1:
        return valid[0]
    if valid:
        console.print("[bold]workflows here:[/bold]")
        for path in valid:
            console.print(f"  graphx tui {path}")
        raise typer.Exit(code=0)
    from importlib.resources import as_file, files
    bundled = files("graphx.examples") / "gpu_report.yaml"
    with as_file(bundled) as path:
        local = Path("gpu_report.yaml")
        if not local.exists():
            local.write_text(path.read_text())
            console.print(f"[green]✔[/green] no workflow here — copied the bundled "
                          f"[bold]gpu_report[/bold] demo to {local}")
        return local


@app.command()
def tui(workflow: Annotated[Path | None, typer.Argument(dir_okay=False,
                                                        help="workflow yaml; omit to "
                                                        "auto-pick or scaffold a demo")] = None,
        trace: Annotated[Path | None, typer.Option(help="play a recorded event trace")] = None,
        db: Annotated[Path | None, typer.Option(help="SQLite db path")] = None,
        attach: Annotated[str | None, typer.Option(help="graphx server URL to attach to, "
                                                   "e.g. http://localhost:8420")] = None,
        thread: Annotated[str | None, typer.Option(help="thread id to follow "
                                                   "(with --attach)")] = None,
        ) -> None:
    """Open the TUI: graph view + live runs (local or attached to a server)."""
    if workflow is None:
        workflow = _default_workflow()
    elif not workflow.exists():
        raise typer.BadParameter(f"no such file: {workflow}")
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
