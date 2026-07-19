"""yaml_writer round-trip: mutations preserve comments and formatting."""

from pathlib import Path

import pytest

from graphx.model.yaml_loader import LoadError, load_graph
from graphx.model.yaml_writer import WorkflowFile

SOURCE = """\
version: 1
name: writer_test
# top comment stays put
state:
  x: { type: int, default: 0 }   # channel comment
entry: [a]
nodes:
  - id: a
    type: function          # inline comment
    handler: "graphx.demo:sysinfo"
  - id: b
    type: shell
    command: ["echo", "hi"]
edges:
  - { from: a, to: b }
  - { from: b, to: end }
"""


@pytest.fixture()
def wf_path(tmp_path: Path) -> Path:
    path = tmp_path / "wf.yaml"
    path.write_text(SOURCE)
    return path


class TestWorkflowFile:
    def test_add_node_and_edge_still_loads(self, wf_path):
        wf = WorkflowFile(wf_path)
        wf.add_node({"id": "c", "type": "shell", "command": ["echo", "new"]}, after="b")
        wf.add_edge("b", "c")
        wf.save()
        graph = load_graph(wf_path)
        assert "c" in graph.nodes
        assert any(e.source == "b" and e.target == "c" for e in graph.edges)

    def test_comments_preserved(self, wf_path):
        wf = WorkflowFile(wf_path)
        wf.add_node({"id": "c", "type": "shell", "command": ["echo", "x"]})
        wf.save()
        text = wf_path.read_text()
        assert "# top comment stays put" in text
        assert "# inline comment" in text
        assert "# channel comment" in text

    def test_remove_node_drops_its_edges(self, wf_path):
        wf = WorkflowFile(wf_path)
        wf.remove_node("b")
        wf.save()
        graph = load_graph(wf_path)
        assert "b" not in graph.nodes
        assert all("b" not in (e.source, e.target) for e in graph.edges)

    def test_update_node(self, wf_path):
        wf = WorkflowFile(wf_path)
        wf.update_node("b", {"command": ["echo", "changed"], "timeout": "5s"})
        wf.save()
        graph = load_graph(wf_path)
        assert graph.nodes["b"].config["command"] == ["echo", "changed"]
        assert graph.nodes["b"].resilience.timeout_s == 5.0

    def test_duplicate_node_id_rejected(self, wf_path):
        wf = WorkflowFile(wf_path)
        with pytest.raises(LoadError):
            wf.add_node({"id": "a", "type": "shell"})

    def test_duplicate_edge_rejected(self, wf_path):
        wf = WorkflowFile(wf_path)
        with pytest.raises(LoadError):
            wf.add_edge("a", "b")

    def test_remove_missing_edge_rejected(self, wf_path):
        wf = WorkflowFile(wf_path)
        with pytest.raises(LoadError):
            wf.remove_edge("a", "ghost")
