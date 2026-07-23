"""Export to a portable folder (wheel build stubbed for speed)."""

import pytest

from graphx.export import _graph_secret_refs, _required_extras, export_workflow
from graphx.model.yaml_loader import load_graph
from graphx.nodes.registry import load_builtin_nodes

load_builtin_nodes()

WF = """\
version: 1
name: exp
entry: [call]
nodes:
  - id: call
    type: api
    method: GET
    url: "https://api.test/x"
    headers: { Authorization: "Bearer secret://api_key" }
edges:
  - { from: call, to: end }
"""


@pytest.fixture(autouse=True)
def _stub_wheel(monkeypatch):
    # avoid the slow real pip build in unit tests
    def fake(out_dir):
        (out_dir / "graphx-0.6.0-py3-none-any.whl").write_bytes(b"stub")
        return "graphx-0.6.0-py3-none-any.whl"
    monkeypatch.setattr("graphx.export._build_wheel", fake)


def test_export_produces_all_files(tmp_path):
    wf = tmp_path / "exp.yaml"
    wf.write_text(WF)
    out = export_workflow(wf, tmp_path / "out", docker=True)
    names = {p.name for p in out.iterdir()}
    assert {"exp.yaml", "run.py", "requirements.txt", ".env.example",
            "README.md", "Dockerfile", "graphx-0.6.0-py3-none-any.whl"} <= names


def test_requirements_reference_the_wheel(tmp_path):
    wf = tmp_path / "exp.yaml"
    wf.write_text(WF)
    out = export_workflow(wf, tmp_path / "out")
    reqs = (out / "requirements.txt").read_text()
    assert reqs.startswith("./graphx-0.6.0-py3-none-any.whl")


def test_env_example_lists_secret_names_not_values(tmp_path):
    wf = tmp_path / "exp.yaml"
    wf.write_text(WF)
    out = export_workflow(wf, tmp_path / "out")
    env = (out / ".env.example").read_text()
    assert "api_key=" in env
    # the runner + readme never contain a secret VALUE (there are none to leak)
    assert "secret://" not in (out / "run.py").read_text()


def test_run_py_targets_the_workflow(tmp_path):
    wf = tmp_path / "exp.yaml"
    wf.write_text(WF)
    out = export_workflow(wf, tmp_path / "out")
    run_py = (out / "run.py").read_text()
    assert 'app(["run"' in run_py and "exp.yaml" in run_py


def test_required_extras_detection(tmp_path):
    mcp_wf = tmp_path / "m.yaml"
    mcp_wf.write_text(
        "version: 1\nname: m\nmcp_servers: { s: { transport: stdio, command: [x] } }\n"
        "entry: [n]\nnodes:\n  - { id: n, type: mcp, server: s, tool: t }\n"
        "edges:\n  - { from: n, to: end }\n")
    graph = load_graph(mcp_wf)
    assert "mcp" in _required_extras(graph)


def test_no_secrets_workflow(tmp_path):
    wf = tmp_path / "plain.yaml"
    wf.write_text("version: 1\nname: plain\nentry: [s]\n"
                  'nodes:\n  - { id: s, type: shell, command: ["echo", "hi"] }\n'
                  "edges:\n  - { from: s, to: end }\n")
    out = export_workflow(wf, tmp_path / "out")
    assert _graph_secret_refs(load_graph(wf)) == set()
    assert "no secrets" in (out / ".env.example").read_text()
