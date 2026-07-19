"""TUI tests: layout, canvas rendering, and the app driving a real run."""

from pathlib import Path

import pytest

pytest.importorskip("textual")
pytest.importorskip("grandalf")

from graphx.model.yaml_loader import load_graph
from graphx.nodes.registry import load_builtin_nodes
from graphx.tui.app import GraphxApp
from graphx.tui.canvas import GraphView, render_graph
from graphx.tui.layout import compute_layout

HELLO = Path(__file__).parents[2] / "examples" / "hello.yaml"

load_builtin_nodes()


@pytest.fixture()
def hello_graph():
    return load_graph(HELLO)


class TestLayout:
    def test_all_nodes_have_boxes(self, hello_graph):
        layout = compute_layout(hello_graph)
        for nid in hello_graph.nodes:
            assert nid in layout.boxes
        assert "end" in layout.boxes  # edges point at end

    def test_no_box_overlap(self, hello_graph):
        layout = compute_layout(hello_graph)
        boxes = list(layout.boxes.values())
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                x_apart = a.x + a.w <= b.x or b.x + b.w <= a.x
                y_apart = a.y + a.h <= b.y or b.y + b.h <= a.y
                assert x_apart or y_apart, f"{a.node_id} overlaps {b.node_id}"

    def test_condition_goto_becomes_layout_edge(self, hello_graph):
        layout = compute_layout(hello_graph)
        pairs = {(r.source, r.target) for r in layout.edges}
        assert ("again", "bump") in pairs      # loop-back from goto
        assert ("again", "done") in pairs      # else branch

    def test_loop_back_flagged(self, hello_graph):
        layout = compute_layout(hello_graph)
        back = {(r.source, r.target) for r in layout.edges if r.back}
        assert ("again", "bump") in back


class TestCanvas:
    def test_render_contains_all_labels(self, hello_graph):
        layout = compute_layout(hello_graph)
        canvas = render_graph(layout, hello_graph)
        text = "\n".join("".join(c.char for c in row) for row in canvas.grid)
        for nid in hello_graph.nodes:
            assert nid in text

    def test_status_changes_style(self, hello_graph):
        layout = compute_layout(hello_graph)
        canvas = render_graph(layout, hello_graph, {"greet": "failed"})
        box = layout.boxes["greet"]
        assert canvas.grid[box.y][box.x].style == "bold red"


class TestApp:
    async def test_mounts_and_selects(self, hello_graph):
        app = GraphxApp(hello_graph, workflow_path=HELLO)
        async with app.run_test(size=(120, 45)) as pilot:
            assert app.query_one("#graph-pane", GraphView)._canvas is not None
            first = app.selected
            await pilot.press("tab")
            assert app.selected != first

    async def test_full_run_inside_tui(self, hello_graph, tmp_path):
        app = GraphxApp(hello_graph, workflow_path=HELLO,
                        db_path=tmp_path / "test.db")
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.statuses.get("done") == "ok"
            assert app.statuses.get("again") == "ok"
            assert not app.running
