"""Palette modals: structured graph mutations that write back to YAML."""

from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option

_MODAL_CSS = """
#modal-box { width: 76; max-height: 90%; border: thick $accent; padding: 1 2;
             background: $surface; }
#modal-buttons { height: auto; align-horizontal: right; }
#modal-buttons Button { margin: 0 1; }
"""


class AddNodeScreen(ModalScreen[dict[str, Any] | None]):
    """Collect id / type / config-YAML for a new node.

    When inference endpoints have been discovered, they are offered as
    one-click `model:` lines for agent/router nodes.
    """

    DEFAULT_CSS = _MODAL_CSS + """
    #config-area { height: 10; }
    #model-list { max-height: 6; }
    #model-hint { color: $text-muted; }
    """

    def __init__(self, endpoints: list | None = None):
        super().__init__()
        self.endpoints = endpoints or []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[bold]add node[/bold]")
            yield Input(placeholder="node id", id="node-id")
            yield Input(placeholder="type (function/shell/agent/api/mcp/condition/"
                                    "map/merge/human/router/wait/subworkflow)",
                        id="node-type")
            yield Label("config (YAML):")
            yield TextArea("", id="config-area", language="yaml")
            model_options = [
                Option(f"{ep.alias}/{model}  [{ep.host}:{ep.port}]".replace("[", "\\["),
                       id=f"{ep.alias}/{model}")
                for ep in self.endpoints for model in ep.models
            ]
            if model_options:
                yield Label("discovered models (click to insert):", id="model-hint")
                yield OptionList(*model_options, id="model-list")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("add", id="ok", variant="primary")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        area = self.query_one("#config-area", TextArea)
        text = area.text.rstrip()
        text = (text + "\n" if text else "") + f"model: {event.option.id}\n"
        area.text = text
        type_input = self.query_one("#node-type", Input)
        if not type_input.value.strip():
            type_input.value = "agent"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        node_id = self.query_one("#node-id", Input).value.strip()
        node_type = self.query_one("#node-type", Input).value.strip()
        raw = self.query_one("#config-area", TextArea).text
        if not node_id or not node_type:
            self.notify("id and type are required", severity="error")
            return
        config: dict[str, Any] = {}
        if raw.strip():
            try:
                config = YAML(typ="safe", pure=True).load(raw) or {}
            except Exception as exc:  # noqa: BLE001 — shown to the user
                self.notify(f"bad YAML: {exc}", severity="error")
                return
            if not isinstance(config, dict):
                self.notify("config must be a YAML mapping", severity="error")
                return
        self.dismiss({"id": node_id, "type": node_type, **config})


class AddApiScreen(ModalScreen[dict[str, Any] | None]):
    """Scaffold an api node from a live service's OpenAPI spec."""

    DEFAULT_CSS = _MODAL_CSS + """
    #op-list { height: 14; }
    #spec-status { color: $text-muted; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._ops: list = []
        self._spec: dict = {}
        self._base_url: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[bold]add api node from OpenAPI[/bold]")
            yield Input(placeholder="node id (blank = use operationId)", id="node-id")
            yield Input(placeholder="service base URL or spec URL "
                                    "(e.g. http://localhost:8420)", id="spec-url")
            yield Label("", id="spec-status")
            yield OptionList(id="op-list")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("fetch spec", id="fetch", variant="primary")

    def _status(self, text: str) -> None:
        self.query_one("#spec-status", Label).update(text)

    async def _fetch(self) -> None:
        from ..model.openapi import SpecError, fetch_spec, parse_operations

        url = self.query_one("#spec-url", Input).value.strip()
        if not url:
            self._status("enter a URL first")
            return
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        self._status(f"fetching {url} …")
        try:
            self._spec, self._base_url = await fetch_spec(url)
        except SpecError as exc:
            self._status(f"[red]{exc}[/red]")
            return
        self._ops = parse_operations(self._spec)
        option_list = self.query_one("#op-list", OptionList)
        option_list.clear_options()
        option_list.add_options(
            [Option(op.label, id=str(i)) for i, op in enumerate(self._ops)])
        title = (self._spec.get("info") or {}).get("title", "spec")
        self._status(f"{title}: {len(self._ops)} operations — pick one")
        option_list.focus()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "fetch":
            await self._fetch()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "spec-url":
            await self._fetch()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        from ..model.openapi import scaffold_api_node

        op = self._ops[int(event.option.id)]
        node_id = self.query_one("#node-id", Input).value.strip() or None
        self.dismiss(scaffold_api_node(op, self._base_url, self._spec, node_id))


class ConnectScreen(ModalScreen[tuple[str, str | None] | None]):
    """Pick a target node (and optional when-clause) for a new edge."""

    DEFAULT_CSS = _MODAL_CSS + """
    #target-list { height: 12; }
    """

    def __init__(self, source: str, targets: list[str]):
        super().__init__()
        self.source = source
        self.targets = targets

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"[bold]connect[/bold] {self.source} → …")
            yield OptionList(*[Option(t, id=t) for t in self.targets], id="target-list")
            yield Input(placeholder="when (optional expression)", id="when-clause")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        when = self.query_one("#when-clause", Input).value.strip() or None
        self.dismiss((event.option.id, when))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class NewWorkflowScreen(ModalScreen[tuple[str, str] | None]):
    """Name + template picker for a brand-new workflow file."""

    DEFAULT_CSS = _MODAL_CSS + """
    #template-list { height: 8; }
    """

    def compose(self) -> ComposeResult:
        from ..templates import TEMPLATES
        with Vertical(id="modal-box"):
            yield Label("[bold]new workflow[/bold]")
            yield Input(placeholder="workflow name (file will be <name>.yaml)",
                        id="wf-name")
            yield Label("template:")
            yield OptionList(
                *[Option(f"{t.key:9} — {t.description}", id=t.key)
                  for t in TEMPLATES.values()],
                id="template-list")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        name = self.query_one("#wf-name", Input).value.strip()
        if not name:
            self.notify("enter a name first", severity="error")
            return
        self.dismiss((name, event.option.id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class PromptScreen(ModalScreen[str | None]):
    """A single free-text prompt (used for NL generate / edit)."""

    DEFAULT_CSS = _MODAL_CSS + """
    #prompt-area { height: 8; }
    """

    def __init__(self, title: str, placeholder: str) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"[bold]{self._title}[/bold]")
            yield TextArea("", id="prompt-area")
            yield Label(f"[dim]{self._placeholder}[/dim]")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("go", id="go", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        text = self.query_one("#prompt-area", TextArea).text.strip()
        if not text:
            self.notify("type something first", severity="error")
            return
        self.dismiss(text)


class AddEndpointScreen(ModalScreen[object | None]):
    """Type an OpenAI-compatible endpoint URL; probe it and return the Endpoint."""

    DEFAULT_CSS = _MODAL_CSS

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[bold]add an inference endpoint[/bold]")
            yield Input(placeholder="URL, e.g. http://192.168.1.50:8000 or "
                                    "http://host:11434", id="endpoint-url")
            yield Label("", id="endpoint-status")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("probe", id="probe", variant="primary")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        url = self.query_one("#endpoint-url", Input).value.strip()
        if not url:
            return
        self.query_one("#endpoint-status", Label).update(f"probing {url}…")
        from ..llm.discovery import add_endpoint
        endpoint = await add_endpoint(url)
        if endpoint is None:
            self.query_one("#endpoint-status", Label).update(
                "[red]no LLM server there[/red]")
            return
        self.dismiss(endpoint)


class ConnectorScreen(ModalScreen[dict | None]):
    """Pick a service connector and fill its fields → returns a render spec."""

    DEFAULT_CSS = _MODAL_CSS + """
    #connector-list { height: 10; }
    #field-rows Input { margin-bottom: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._connector = None

    def compose(self) -> ComposeResult:
        from ..connectors.registry import all_connectors
        with Vertical(id="modal-box"):
            yield Label("[bold]add a service connector[/bold]")
            yield OptionList(
                *[Option(f"{c.category:9} {c.key:16} {c.description}", id=c.key)
                  for c in all_connectors()],
                id="connector-list")
            yield Input(placeholder="node id (blank = connector key)", id="conn-id")
            yield Vertical(id="field-rows")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("add", id="add", variant="primary")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        from ..connectors.registry import get
        self._connector = get(event.option.id)
        rows = self.query_one("#field-rows", Vertical)
        rows.remove_children()
        for field in self._connector.fields:
            required = "" if field.required and field.default is None else " (optional)"
            rows.mount(Input(placeholder=f"{field.name}{required}: {field.placeholder}",
                             id=f"field-{field.name}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" or self._connector is None:
            self.dismiss(None)
            return
        values: dict = {}
        for field in self._connector.fields:
            raw = self.query_one(f"#field-{field.name}", Input).value.strip()
            if raw:
                values[field.name] = raw
        missing = self._connector.missing_fields(values)
        if missing:
            self.notify("missing: " + ", ".join(missing), severity="error")
            return
        node_id = self.query_one("#conn-id", Input).value.strip() or self._connector.key
        self.dismiss({"connector": self._connector, "node_id": node_id, "values": values})


class MissingSecretsScreen(ModalScreen[bool]):
    """Prompt for each secret a workflow needs but that isn't set yet.
    Saves entered values to the store; returns True if all were filled."""

    DEFAULT_CSS = _MODAL_CSS + """
    #secret-rows Input { margin-bottom: 1; }
    """

    def __init__(self, names: list[str], store: Any):
        super().__init__()
        self.names = names
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[bold]this workflow needs credentials[/bold]")
            yield Label("[dim]stored 0600 in ~/.graphx/secrets.json; never written to "
                        "the workflow file[/dim]")
            with Vertical(id="secret-rows"):
                for name in self.names:
                    yield Input(placeholder=f"secret://{name}", password=True,
                                id=f"secret-{name}")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("save", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)
            return
        for name in self.names:
            value = self.query_one(f"#secret-{name}", Input).value
            if value:
                self.store.set(name, value)
        still_missing = [n for n in self.names if self.store.get(n) is None]
        if still_missing:
            self.notify("still missing: " + ", ".join(still_missing), severity="error")
            return
        self.dismiss(True)


class SecretsScreen(ModalScreen[None]):
    """Manage stored secrets: list names (masked), add, delete."""

    DEFAULT_CSS = _MODAL_CSS + """
    #secret-list { height: 8; }
    """

    def __init__(self, store: Any):
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(f"[bold]secrets[/bold] [dim]({self.store.backend()} backend)[/dim]")
            yield OptionList(*self._options(), id="secret-list")
            yield Input(placeholder="new secret name", id="new-name")
            yield Input(placeholder="value", password=True, id="new-value")
            with Horizontal(id="modal-buttons"):
                yield Button("close", id="close")
                yield Button("add", id="add", variant="primary")
            yield Label("[dim]select a row to delete it[/dim]")

    def _options(self) -> list[Option]:
        names = self.store.names()
        if not names:
            return [Option("(none stored)", id="__none__")]
        return [Option(f"{n}   secret://{n}", id=n) for n in names]

    def _refresh(self) -> None:
        option_list = self.query_one("#secret-list", OptionList)
        option_list.clear_options()
        option_list.add_options(self._options())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
            return
        name = self.query_one("#new-name", Input).value.strip()
        value = self.query_one("#new-value", Input).value
        if not name or not value:
            self.notify("name and value required", severity="error")
            return
        try:
            self.store.set(name, value)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self.query_one("#new-name", Input).value = ""
        self.query_one("#new-value", Input).value = ""
        self._refresh()
        self.notify(f"stored {name}")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id != "__none__":
            self.store.delete(event.option.id)
            self._refresh()
            self.notify(f"removed {event.option.id}")


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = _MODAL_CSS

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.question)
            with Horizontal(id="modal-buttons"):
                yield Button("no", id="no")
                yield Button("yes", id="yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")
