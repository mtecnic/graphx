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


@pytest.fixture(autouse=True)
def _isolated_discovery(tmp_path_factory, monkeypatch):
    # keep the discovery worker away from the real cache, LAN, and network
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path_factory.mktemp("gxhome")))
    monkeypatch.setenv("GRAPHX_NO_LAN_SCAN", "1")
    monkeypatch.setenv("GRAPHX_NO_DISCOVERY", "1")


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

    async def test_new_workflow_via_n_key(self, hello_graph, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("GRAPHX_NO_LAN_SCAN", "1")
        wf = tmp_path / "hello.yaml"
        shutil.copy(HELLO, wf)
        app = GraphxApp(hello_graph, workflow_path=wf)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            await pilot.click("#wf-name")
            for ch in "fresh":
                await pilot.press(ch)
            await pilot.click("#template-list")
            await pilot.press("enter")          # first option: blank
            await pilot.pause()
            assert (tmp_path / "fresh.yaml").exists()
            assert app.graph.name == "fresh"
            assert app.workflow_path == tmp_path / "fresh.yaml"

    async def test_add_node_offers_discovered_models(self, hello_graph, tmp_path):
        import time as _time

        from graphx.llm.discovery import Endpoint
        from graphx.tui.palette import AddNodeScreen
        app = GraphxApp(hello_graph, workflow_path=HELLO)
        app.endpoints = [Endpoint(base_url="http://127.0.0.1:8000/v1",
                                  kind="openai", host="127.0.0.1", port=8000,
                                  models=("test-model",), checked_at=_time.time())]
        async with app.run_test(size=(120, 45)) as pilot:
            app._palette_add_node()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, AddNodeScreen)
            option_list = screen.query_one("#model-list")
            assert option_list.option_count == 1
            await pilot.click("#model-list")
            await pilot.press("enter")
            area = screen.query_one("#config-area")
            assert "model: openai_local_8000/test-model" in area.text
            await pilot.press("escape")

    async def test_add_agent_node_injects_provider(self, tmp_path, monkeypatch):
        import shutil
        import time as _time

        from graphx.llm.discovery import Endpoint
        from graphx.model.yaml_loader import load_graph
        wf = tmp_path / "hello.yaml"
        shutil.copy(HELLO, wf)
        app = GraphxApp(load_graph(wf), workflow_path=wf)
        app.endpoints = [Endpoint(base_url="http://127.0.0.1:8000/v1", kind="openai",
                                  host="127.0.0.1", port=8000, models=("qwen-x",),
                                  checked_at=_time.time())]
        async with app.run_test(size=(120, 45)) as pilot:
            node = {"id": "ai", "type": "agent",
                    "model": "openai_local_8000/qwen-x", "prompt": "hi"}
            provider = app._provider_for_model(node)
            assert provider[0] == "openai_local_8000"

            def mutate(w):
                w.add_node(node, after="greet")
                w.set_provider(*provider)
            app._mutate(mutate)
            await pilot.pause()
        graph = load_graph(wf)
        assert "ai" in graph.nodes
        assert graph.providers["openai_local_8000"]["base_url"] == \
            "http://127.0.0.1:8000/v1"

    async def test_secrets_screen_opens_and_stores(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
        from graphx.tui.palette import SecretsScreen
        app = GraphxApp(load_graph(HELLO), workflow_path=HELLO)
        async with app.run_test(size=(120, 45)) as pilot:
            app._open_secrets()
            await pilot.pause()
            assert isinstance(app.screen, SecretsScreen)
            app.screen.query_one("#new-name").value = "apikey"
            app.screen.query_one("#new-value").value = "sk-secret"
            await pilot.click("#add")
            await pilot.pause()
            assert app.secret_store.get("apikey") == "sk-secret"
            await pilot.press("escape")

    async def test_missing_secret_prompt_before_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
        wf = tmp_path / "needs.yaml"
        wf.write_text(
            "version: 1\nname: needs\nentry: [c]\n"
            "nodes:\n  - id: c\n    type: api\n    method: GET\n"
            '    url: "https://api.test/x"\n'
            '    headers: { Authorization: "Bearer secret://tok" }\n'
            "edges:\n  - { from: c, to: end }\n")
        from graphx.tui.palette import MissingSecretsScreen
        app = GraphxApp(load_graph(wf), workflow_path=wf)
        async with app.run_test(size=(120, 45)) as pilot:
            app._run_with_secrets()
            await pilot.pause()
            assert isinstance(app.screen, MissingSecretsScreen)
            app.screen.query_one("#secret-tok").value = "resolved-token"
            await pilot.click("#save")
            await pilot.pause()
            assert app.secret_store.get("tok") == "resolved-token"

    async def test_connector_palette_inserts_node(self, tmp_path, monkeypatch):
        import shutil
        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
        wf = tmp_path / "hello.yaml"
        shutil.copy(HELLO, wf)
        from graphx.connectors.registry import all_connectors
        from graphx.tui.palette import ConnectorScreen
        slack_index = [c.key for c in all_connectors()].index("slack")
        app = GraphxApp(load_graph(wf), workflow_path=wf)
        async with app.run_test(size=(120, 45)) as pilot:
            app._palette_connector()
            await pilot.pause()
            assert isinstance(app.screen, ConnectorScreen)
            option_list = app.screen.query_one("#connector-list")
            option_list.highlighted = slack_index
            await pilot.press("enter")          # selecting mounts the field inputs
            await pilot.pause()
            app.screen.query_one("#field-message").value = "ping"
            app.screen.query_one("#conn-id").value = "notify"
            await pilot.click("#add")
            await pilot.pause()
        graph = load_graph(wf)
        assert graph.nodes["notify"].config["url"] == "secret://slack_webhook_url"

    async def test_generate_writes_and_loads_workflow(self, tmp_path, monkeypatch):
        import shutil

        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
        wf = tmp_path / "hello.yaml"
        shutil.copy(HELLO, wf)

        # stub the builder so no LLM is needed
        from graphx.builder.draft import WorkflowDraft
        from graphx.builder.result import BuildResult
        generated = ("version: 1\nname: made\nentry: [s]\n"
                     'nodes:\n  - {id: s, type: shell, command: ["echo", "x"]}\n'
                     "edges:\n  - {from: s, to: end}\n")
        draft = WorkflowDraft.from_dict(__import__("ruamel.yaml", fromlist=["YAML"])
                                        .YAML(typ="safe", pure=True).load(generated))

        async def fake_build(**kwargs):
            return BuildResult(ok=True, yaml=generated, draft=draft)
        monkeypatch.setattr("graphx.builder.build_workflow", fake_build)

        import time as _time

        from graphx.llm.discovery import Endpoint
        from graphx.tui.palette import PromptScreen
        app = GraphxApp(load_graph(wf), workflow_path=wf)
        app.endpoints = [Endpoint(base_url="http://127.0.0.1:8000/v1", kind="openai",
                                  host="127.0.0.1", port=8000, models=("m",),
                                  checked_at=_time.time())]
        async with app.run_test(size=(120, 45)) as pilot:
            app._palette_generate()
            await pilot.pause()
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#prompt-area").text = "echo x"
            await pilot.click("#go")
            await pilot.pause()
        assert (tmp_path / "made.yaml").exists()
        assert app.graph.name == "made"

    async def test_full_run_inside_tui(self, hello_graph, tmp_path):
        app = GraphxApp(hello_graph, workflow_path=HELLO,
                        db_path=tmp_path / "test.db")
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("r")
            for _ in range(200):                  # poll: the run worker sets running=False
                await pilot.pause()
                if not app.running and app.statuses.get("done") == "ok":
                    break
            assert app.statuses.get("done") == "ok"
            assert app.statuses.get("again") == "ok"
            assert not app.running
