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
    """Collect id / type / config-YAML for a new node."""

    DEFAULT_CSS = _MODAL_CSS + """
    #config-area { height: 12; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("[bold]add node[/bold]")
            yield Input(placeholder="node id", id="node-id")
            yield Input(placeholder="type (function/shell/agent/api/mcp/condition/"
                                    "map/merge/human/router/wait/subworkflow)",
                        id="node-type")
            yield Label("config (YAML):")
            yield TextArea("", id="config-area", language="yaml")
            with Horizontal(id="modal-buttons"):
                yield Button("cancel", id="cancel")
                yield Button("add", id="ok", variant="primary")

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
