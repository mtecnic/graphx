"""Character-grid graph rendering + the scrollable Textual widget.

Edges are drawn first (dim lines), boxes over them. Downward edges run
bottom-anchor → mid-lane → top-anchor; loop-backs route around the
right side of the drawing. Node status drives the box color, matching
the Dagger-style run view.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.segment import Segment
from rich.style import Style
from textual.scroll_view import ScrollView
from textual.geometry import Size
from textual.strip import Strip

from ..model.graph import END, Graph
from .layout import GraphLayout, NodeBox

STATUS_STYLES: dict[str, str] = {
    "idle": "white",
    "queued": "cyan",
    "running": "bold yellow",
    "ok": "green",
    "failed": "bold red",
    "cached": "blue",
    "degraded": "yellow",
    "waiting": "bold magenta",
    "skipped": "dim",
}

STATUS_ICONS: dict[str, str] = {
    "idle": " ", "queued": "…", "running": "▶", "ok": "✔",
    "failed": "✘", "cached": "≡", "degraded": "⚠", "waiting": "⏸", "skipped": "·",
}


@dataclass
class Cell:
    char: str = " "
    style: str = ""


class CharCanvas:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.grid: list[list[Cell]] = [[Cell() for _ in range(width)] for _ in range(height)]

    def put(self, x: int, y: int, char: str, style: str = "") -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.grid[y][x] = Cell(char, style)

    def text(self, x: int, y: int, text: str, style: str = "") -> None:
        for i, ch in enumerate(text):
            self.put(x + i, y, ch, style)

    def vline(self, x: int, y0: int, y1: int, style: str = "") -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            existing = self.grid[y][x].char if 0 <= y < self.height and 0 <= x < self.width else " "
            self.put(x, y, "┼" if existing == "─" else "│", style)

    def hline(self, x0: int, x1: int, y: int, style: str = "") -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            existing = self.grid[y][x].char if 0 <= y < self.height and 0 <= x < self.width else " "
            self.put(x, y, "┼" if existing == "│" else "─", style)

    def box(self, b: NodeBox, title: str, subtitle: str, style: str,
            icon: str = " ", selected: bool = False) -> None:
        h_edge = "═" if selected else "─"
        v_edge = "║" if selected else "│"
        corners = "╔╗╚╝" if selected else "┌┐└┘"
        self.put(b.x, b.y, corners[0], style)
        self.put(b.x + b.w - 1, b.y, corners[1], style)
        self.put(b.x, b.y + b.h - 1, corners[2], style)
        self.put(b.x + b.w - 1, b.y + b.h - 1, corners[3], style)
        for x in range(b.x + 1, b.x + b.w - 1):
            self.put(x, b.y, h_edge, style)
            self.put(x, b.y + b.h - 1, h_edge, style)
        for y in range(b.y + 1, b.y + b.h - 1):
            self.put(b.x, y, v_edge, style)
            self.put(b.x + b.w - 1, y, v_edge, style)
        inner = b.w - 2
        label = f"{icon} {title}"[:inner].center(inner)
        self.text(b.x + 1, b.y + 1, label, style)
        if b.h > 2 and subtitle:
            pass  # BOX_H=3 keeps one content row; subtitle folded into title styles


def render_graph(layout: GraphLayout, graph: Graph,
                 statuses: dict[str, str] | None = None,
                 selected: str | None = None) -> CharCanvas:
    statuses = statuses or {}
    canvas = CharCanvas(layout.width, layout.height)
    edge_style = "dim"
    lane = layout.width - 4

    for route in layout.edges:
        src, dst = layout.boxes[route.source], layout.boxes[route.target]
        style = "red dim" if route.kind == "on_error" else edge_style
        sx, sy = src.bottom_anchor
        tx, ty = dst.top_anchor
        if not route.back:
            mid = sy + max(1, (ty - sy) // 2)
            canvas.vline(sx, sy + 1, mid, style)
            if tx != sx:
                canvas.hline(sx, tx, mid, style)
                canvas.put(sx, mid, "└" if tx > sx else "┘", style)
                canvas.put(tx, mid, "┐" if tx > sx else "┌", style)
            canvas.vline(tx, mid + (1 if tx != sx else 0), ty - 1, style)
            canvas.put(tx, ty - 1, "▼", style)
        else:
            # loop-back: out the bottom, around the right lane, into the top
            row_out, row_in = sy + 1, ty - 1
            canvas.hline(sx, lane, row_out, style)
            canvas.put(sx, row_out, "└", style)
            canvas.hline(tx, lane, row_in, style)
            canvas.vline(lane, row_in, row_out, style)
            canvas.put(lane, row_out, "┘", style)
            canvas.put(lane, row_in, "┐", style)
            canvas.put(tx, row_in, "▼", style)
            lane -= 1

    for nid, box in layout.boxes.items():
        if nid == END:
            canvas.box(box, "end", "", "dim", " ")
            continue
        status = statuses.get(nid, "idle")
        style = STATUS_STYLES.get(status, "white")
        node = graph.nodes[nid]
        title = f"{nid}·{node.type}" if len(nid) + len(node.type) + 3 < box.w else nid
        canvas.box(box, title, node.type, style,
                   STATUS_ICONS.get(status, " "), selected=nid == selected)
    return canvas


class GraphView(ScrollView):
    """Scrollable rendering of a CharCanvas."""

    DEFAULT_CSS = """
    GraphView { background: $surface; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._canvas: CharCanvas | None = None

    def set_canvas(self, canvas: CharCanvas) -> None:
        self._canvas = canvas
        self.virtual_size = Size(canvas.width, canvas.height)
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        row = y + scroll_y
        if self._canvas is None or row >= self._canvas.height:
            return Strip.blank(self.size.width)
        cells = self._canvas.grid[row]
        segments = []
        i = 0
        while i < len(cells):
            style = cells[i].style
            j = i
            while j < len(cells) and cells[j].style == style:
                j += 1
            text = "".join(cell.char for cell in cells[i:j])
            segments.append(Segment(text, Style.parse(style) if style else None))
            i = j
        return Strip(segments).crop(scroll_x, scroll_x + self.size.width)
