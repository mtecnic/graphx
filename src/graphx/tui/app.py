"""GraphxApp: lazygit-style shell — graph pane, node detail, logs, footer.

Run view follows the Dagger pattern: per-node status icons/colors on the
graph itself, streaming logs below, human gates as modal prompts.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from ..engine.events import EventBus, EventType, RunEvent
from ..engine.executor import Executor
from ..engine.interrupts import Command
from ..model.graph import Graph
from ..model.yaml_loader import load_graph
from .canvas import GraphView, render_graph
from .layout import GraphLayout, compute_layout


def _esc(text: str) -> str:
    """Escape ALL brackets for markup contexts.

    rich.markup.escape only escapes tag-looking brackets, but Textual's
    Content parser is stricter (e.g. `["a", "b=c"]` parses as a tag) —
    so data interpolated into any markup string goes through this.
    """
    return text.replace("[", "\\[")


class GateScreen(ModalScreen[str]):
    """Modal for a human-in-the-loop gate."""

    DEFAULT_CSS = """
    GateScreen { align: center middle; }
    #gate-box { width: 70; max-height: 80%; border: thick $accent; padding: 1 2;
                background: $surface; }
    #gate-buttons { height: auto; align-horizontal: center; }
    #gate-buttons Button { margin: 0 1; }
    #gate-payload { max-height: 12; overflow-y: auto; color: $text-muted; }
    """

    def __init__(self, prompt: str, choices: list[str] | None, payload: Any):
        super().__init__()
        self.prompt = prompt
        self.choices = choices
        self.payload = payload

    def compose(self) -> ComposeResult:
        
        with Vertical(id="gate-box"):
            yield Label(f"⏸  {_esc(self.prompt)}", id="gate-prompt")
            if self.payload is not None:
                yield Static(_esc(json.dumps(self.payload, indent=2,
                                               default=str)[:2000]),
                             id="gate-payload")
            if self.choices:
                with Horizontal(id="gate-buttons"):
                    for choice in self.choices:
                        yield Button(choice, id=f"choice-{choice}")
            else:
                yield Input(placeholder="answer…", id="gate-input")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.label))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class GraphxApp(App):
    TITLE = "graphx"

    CSS = """
    #main { height: 1fr; }
    #graph-pane { width: 2fr; border: round $primary; }
    #side { width: 1fr; }
    #detail { height: 1fr; border: round $secondary; padding: 0 1; overflow-y: auto; }
    #logs { height: 1fr; border: round $secondary; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "run", "run"),
        Binding("tab", "next_node", "next node", show=False, priority=True),
        Binding("shift+tab", "prev_node", "prev node", show=False, priority=True),
        Binding("e", "edit", "edit yaml"),
        Binding("l", "reload", "reload"),
        Binding("n", "new_workflow", "new"),
        Binding("a", "add_node", "add node"),
        Binding("o", "add_api", "api from spec"),
        Binding("c", "connect", "connect"),
        Binding("d", "delete_node", "delete", show=False),
        Binding("p", "play_trace", "play trace", show=False),
    ]

    def __init__(self, graph: Graph, workflow_path: Path,
                 trace_path: Path | None = None, db_path: Path | None = None,
                 attach_url: str | None = None, attach_thread: str | None = None):
        super().__init__()
        self.graph = graph
        self.workflow_path = workflow_path
        self.trace_path = trace_path
        self.db_path = db_path
        self.attach_url = attach_url.rstrip("/") if attach_url else None
        self.attach_thread = attach_thread
        self.layout_: GraphLayout = compute_layout(graph)
        self.statuses: dict[str, str] = {}
        self.node_order = list(graph.nodes)
        self.selected: str | None = self.node_order[0] if self.node_order else None
        self.last_outputs: dict[str, Any] = {}
        self.running = False
        self.endpoints: list = []      # discovered inference endpoints

    # ------------------------------------------------------------- compose

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield GraphView(id="graph-pane")
            with Vertical(id="side"):
                yield Static(id="detail")
                yield RichLog(id="logs", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{self.graph.name} — {self.workflow_path}"
        self.redraw()
        self.update_detail()
        self.watch_file()
        self.discover_endpoints()
        if self.trace_path:
            self.play_trace()
        elif self.attach_url and self.attach_thread:
            self.attach_stream()

    @work(exclusive=True, group="discovery")
    async def discover_endpoints(self) -> None:
        """Background LLM endpoint scan (localhost + LAN) on startup."""
        import os
        if os.environ.get("GRAPHX_NO_DISCOVERY"):
            return
        from ..llm.discovery import discover, lan_scan_enabled
        try:
            self.endpoints = await discover(lan=True)
        except Exception:  # noqa: BLE001 — discovery must never break the TUI
            return
        if self.endpoints:
            models = sum(len(e.models) for e in self.endpoints)
            self.notify(f"🔍 found {len(self.endpoints)} inference endpoint(s), "
                        f"{models} model(s) — offered when adding agent nodes",
                        timeout=6)
        elif lan_scan_enabled():
            self.notify("no LLM endpoints found on localhost or LAN", timeout=4)

    @work(exclusive=True, group="watcher")
    async def watch_file(self) -> None:
        """Mermaid-style: edit the YAML anywhere, the view follows."""
        try:
            from watchfiles import awatch
        except ImportError:
            return
        async for _changes in awatch(self.workflow_path):
            self.action_reload()

    # -------------------------------------------------------------- render

    def redraw(self) -> None:
        canvas = render_graph(self.layout_, self.graph, self.statuses, self.selected)
        self.query_one("#graph-pane", GraphView).set_canvas(canvas)

    def update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if not self.selected or self.selected not in self.graph.nodes:
            detail.update("")
            return
        

        node = self.graph.nodes[self.selected]
        status = self.statuses.get(node.id, "idle")
        lines = [f"[bold]{_esc(node.id)}[/bold] [dim]({node.type})[/dim]  {status}",
                 "", "[bold]config[/bold]"]
        for key, value in node.config.items():
            lines.append(_esc(f"  {key}: {json.dumps(value, default=str)[:120]}"))
        if node.updates:
            lines.append(_esc(
                f"  updates: {json.dumps(dict(node.updates), default=str)[:120]}"))
        if node.max_iterations:
            lines.append(f"  max_iterations: {node.max_iterations}")
        output = self.last_outputs.get(node.id)
        if output is not None:
            lines += ["", "[bold]last output[/bold]",
                      _esc(json.dumps(output, indent=2, default=str)[:1500])]
        detail.update("\n".join(lines))

    def log_line(self, text: str) -> None:
        self.query_one("#logs", RichLog).write(text)

    # -------------------------------------------------------------- events

    def apply_event(self, event: RunEvent) -> None:
        

        node = event.node_id
        etype = event.type
        if etype == EventType.SUPERSTEP_STARTED:
            for nid in event.data.get("frontier", []):
                if self.statuses.get(nid) not in ("running",):
                    self.statuses[nid] = "queued"
        elif etype == EventType.NODE_STARTED and node:
            self.statuses[node] = "running"
        elif etype == EventType.NODE_FINISHED and node:
            data = event.data
            self.statuses[node] = ("degraded" if data.get("degraded")
                                   else "cached" if data.get("cached") else "ok")
            self.last_outputs[node] = data.get("output")
            self.log_line(f"[green]✔[/green] [bold]{node}[/bold] "
                          f"[dim]{data.get('duration_s', '')}[/dim]")
        elif etype == EventType.NODE_FAILED and node:
            self.statuses[node] = "failed"
            self.log_line(f"[red]✘ {node}: "
                          f"{_esc(event.data.get('error', '')[:200])}[/red]")
        elif etype == EventType.NODE_RETRYING and node:
            self.log_line(f"[yellow]↻ {node} retry #{event.data.get('attempt')} "
                          f"in {event.data.get('delay_s')}s[/yellow]")
        elif etype == EventType.NODE_FALLBACK and node:
            self.log_line(f"[yellow]⇄ {node} → {event.data.get('to')}[/yellow]")
        elif etype == EventType.NODE_OUTPUT_CHUNK and node:
            chunk = event.data.get("chunk", "").rstrip()
            if chunk:
                self.log_line(f"[dim]{node} |[/dim] {_esc(chunk)}")
        elif etype == EventType.INTERRUPT_RAISED and node:
            self.statuses[node] = "waiting"
        elif etype == EventType.STATE_UPDATED:
            pass
        elif etype in (EventType.RUN_FINISHED, EventType.RUN_FAILED,
                       EventType.GUARD_TRIPPED):
            style = "green" if etype == EventType.RUN_FINISHED else "red"
            self.log_line(f"[{style}]■ {etype.value} "
                          f"{_esc(json.dumps(event.data, default=str)[:300])}[/{style}]")
        self.redraw()
        if node == self.selected:
            self.update_detail()

    # ------------------------------------------------------------- actions

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # tab cycles nodes on the main screen only; in modals it must
        # keep doing focus traversal
        if action in ("next_node", "prev_node") and len(self.screen_stack) > 1:
            return False
        return True

    def action_next_node(self) -> None:
        if self.node_order:
            i = self.node_order.index(self.selected) if self.selected in self.node_order else -1
            self.selected = self.node_order[(i + 1) % len(self.node_order)]
            self.redraw()
            self.update_detail()

    def action_prev_node(self) -> None:
        if self.node_order:
            i = self.node_order.index(self.selected) if self.selected in self.node_order else 1
            self.selected = self.node_order[(i - 1) % len(self.node_order)]
            self.redraw()
            self.update_detail()

    def action_reload(self) -> None:
        try:
            self.graph = load_graph(self.workflow_path)
        except Exception as exc:  # noqa: BLE001 — surfaced to the user
            self.notify(f"reload failed: {exc}", severity="error", timeout=6)
            return
        self.layout_ = compute_layout(self.graph)
        self.node_order = list(self.graph.nodes)
        self.statuses.clear()
        self.last_outputs.clear()
        self.redraw()
        self.update_detail()
        self.notify("workflow reloaded")

    def action_edit(self) -> None:
        editor = os.environ.get("EDITOR", "vi")
        with self.suspend():
            os.system(f'{editor} "{self.workflow_path}"')  # noqa: S605
        self.action_reload()

    def action_run(self) -> None:
        if self.running:
            self.notify("a run is already in progress", severity="warning")
            return
        self.run_workflow()

    # ------------------------------------------------------------ palette

    def _mutate(self, mutation) -> None:
        """Apply a WorkflowFile mutation and reload from disk."""
        from ..model.yaml_writer import WorkflowFile
        try:
            wf = WorkflowFile(self.workflow_path)
            mutation(wf)
            wf.save()
        except Exception as exc:  # noqa: BLE001 — surfaced to the user
            self.notify(f"edit failed: {exc}", severity="error", timeout=6)
            return
        self.action_reload()

    def _provider_for_model(self, node: dict) -> tuple[str, dict] | None:
        """If the node's model uses a discovered endpoint alias, return
        (alias, provider_config) so the workflow gets a providers: entry."""
        model = node.get("model")
        if not isinstance(model, str) or "/" not in model:
            return None
        alias = model.split("/", 1)[0]
        for endpoint in self.endpoints:
            if endpoint.alias == alias:
                return alias, endpoint.provider_config()
        return None

    @work(exclusive=False)
    async def _palette_add_node(self) -> None:
        from .palette import AddNodeScreen
        node = await self.push_screen_wait(AddNodeScreen(endpoints=self.endpoints))
        if node is None:
            return
        selected = self.selected
        provider = self._provider_for_model(node)

        def mutate(wf) -> None:
            wf.add_node(node, after=selected)
            if provider is not None:
                wf.set_provider(provider[0], provider[1])

        self._mutate(mutate)
        if node["id"] in self.graph.nodes:
            self.selected = node["id"]
            self.redraw()
            self.update_detail()

    @work(exclusive=False)
    async def _palette_add_api(self) -> None:
        from .palette import AddApiScreen
        node = await self.push_screen_wait(AddApiScreen())
        if node is None:
            return
        selected = self.selected
        self._mutate(lambda wf: wf.add_node(node, after=selected))
        if node["id"] in self.graph.nodes:
            self.selected = node["id"]
            self.redraw()
            self.update_detail()
            self.notify(f"api node '{node['id']}' scaffolded — review its "
                        "<state.…> placeholders", timeout=6)

    def action_add_api(self) -> None:
        self._palette_add_api()

    @work(exclusive=False)
    async def _palette_connect(self) -> None:
        from .palette import ConnectScreen
        if not self.selected or self.selected not in self.graph.nodes:
            self.notify("select a source node first (tab)", severity="warning")
            return
        targets = [n for n in self.graph.nodes if n != self.selected] + ["end"]
        result = await self.push_screen_wait(ConnectScreen(self.selected, targets))
        if result is None:
            return
        target, when = result
        source = self.selected
        self._mutate(lambda wf: wf.add_edge(source, target, when))

    @work(exclusive=False)
    async def _palette_delete(self) -> None:
        from .palette import ConfirmScreen
        if not self.selected or self.selected not in self.graph.nodes:
            return
        node_id = self.selected
        if not await self.push_screen_wait(
                ConfirmScreen(f"Delete node '{node_id}' and its edges?")):
            return
        self._mutate(lambda wf: wf.remove_node(node_id))

    def action_add_node(self) -> None:
        self._palette_add_node()

    @work(exclusive=False)
    async def _palette_new_workflow(self) -> None:
        from ..templates import create_workflow
        from .palette import NewWorkflowScreen
        result = await self.push_screen_wait(NewWorkflowScreen())
        if result is None:
            return
        name, template_key = result
        try:
            path = create_workflow(name, template_key,
                                   directory=self.workflow_path.parent,
                                   endpoints=self.endpoints)
        except (ValueError, FileExistsError) as exc:
            self.notify(f"new workflow failed: {exc}", severity="error", timeout=6)
            return
        self.workflow_path = path
        self.sub_title = f"{name} — {path}"
        self.action_reload()
        self.watch_file()   # exclusive group replaces the old watcher
        self.notify(f"created {path.name} — press r to run, e to edit")

    def action_new_workflow(self) -> None:
        self._palette_new_workflow()

    def action_connect(self) -> None:
        self._palette_connect()

    def action_delete_node(self) -> None:
        self._palette_delete()

    # --------------------------------------------------------------- runs

    @work(exclusive=True)
    async def run_workflow(self) -> None:
        from ..persistence.db import open_db
        from ..persistence.sqlite_checkpointer import SqliteCheckpointer

        self.running = True
        self.statuses = {nid: "idle" for nid in self.graph.nodes}
        self.last_outputs.clear()
        thread_id = uuid4().hex[:12]
        self.log_line(f"[bold]— run started (thread {thread_id}) —[/bold]")

        from ..runtime import graph_services

        db = await open_db(self.db_path or ".graphx/graphx.db")
        try:
            async with graph_services(self.graph) as services:
                checkpointer = SqliteCheckpointer(db)
                next_input: dict[str, Any] | Command = {}
                while True:
                    bus = EventBus(run_id=uuid4().hex[:12], thread_id=thread_id,
                                   sink=checkpointer.event_sink)

                    async def pump() -> None:
                        async for event in bus.subscribe():
                            self.apply_event(event)

                    pump_task = asyncio.create_task(pump())
                    executor = Executor(self.graph, checkpointer, bus, services)
                    try:
                        outcome = await executor.run(thread_id, next_input)
                    finally:
                        bus.close()
                        await pump_task

                    if outcome.status != "interrupted":
                        self.log_line(f"[bold]— {outcome.status} after "
                                      f"{outcome.step} steps —[/bold]")
                        break
                    payload = outcome.interrupt.payload or {}
                    answer = await self.push_screen_wait(GateScreen(
                        payload.get("prompt", "input required"),
                        payload.get("choices"), payload.get("payload")))
                    next_input = Command(resume=answer)
        finally:
            await db.close()
            self.running = False

    @work(exclusive=True)
    async def attach_stream(self) -> None:
        """Follow a run on a graphx server over SSE; answer gates remotely."""
        import httpx

        url = f"{self.attach_url}/runs/{self.attach_thread}/events"
        self.log_line(f"[bold]— attached to {url} —[/bold]")
        self.statuses = {nid: "idle" for nid in self.graph.nodes}
        try:
            async with httpx.AsyncClient(timeout=None) as client, \
                    client.stream("GET", url) as response:
                if response.status_code >= 400:
                    self.notify(f"attach failed: HTTP {response.status_code}",
                                severity="error")
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        event = RunEvent.model_validate_json(data)
                    except ValueError:
                        continue
                    self.apply_event(event)
                    if event.type == EventType.INTERRUPT_RAISED:
                        await self._answer_remote_gate(event)
        except httpx.HTTPError as exc:
            self.notify(f"attach stream ended: {exc}", severity="warning")

    async def _answer_remote_gate(self, event: RunEvent) -> None:
        import httpx

        payload = event.data.get("payload") or {}
        answer = await self.push_screen_wait(GateScreen(
            payload.get("prompt", "input required"),
            payload.get("choices"), payload.get("payload")))
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.attach_url}/runs/{self.attach_thread}/resume",
                json={"answer": answer, "workflow": str(self.workflow_path)})
            if response.status_code >= 400:
                self.notify(f"resume failed: {response.text[:200]}", severity="error")
            else:
                self.log_line(f"[magenta]⏵ answered gate: {answer}[/magenta]")
                self.attach_stream()  # re-attach to the resumed run segment

    @work(exclusive=True)
    async def play_trace(self) -> None:
        """Replay a recorded JSONL event trace (no engine involved)."""
        if not self.trace_path:
            return
        self.log_line(f"[bold]— playing trace {self.trace_path} —[/bold]")
        for line in Path(self.trace_path).read_text().splitlines():
            if not line.strip():
                continue
            event = RunEvent.model_validate_json(line)
            self.apply_event(event)
            await asyncio.sleep(0.05)

    def action_play_trace(self) -> None:
        self.play_trace()
