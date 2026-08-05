"""graphx new: templates render valid workflows; discovery fills the agent."""

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graphx.cli import app as cli_app
from graphx.llm.discovery import Endpoint
from graphx.model.validate import has_errors, validate_graph
from graphx.model.yaml_loader import load_graph
from graphx.nodes.registry import known_types, load_builtin_nodes
from graphx.templates import TEMPLATES, create_workflow

load_builtin_nodes()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GRAPHX_NO_LAN_SCAN", "1")


def endpoint():
    return Endpoint(base_url="http://127.0.0.1:8000/v1", kind="openai",
                    host="127.0.0.1", port=8000,
                    models=("test-model",), checked_at=time.time())


class TestTemplates:
    @pytest.mark.parametrize("key", list(TEMPLATES))
    def test_every_template_renders_valid(self, key, tmp_path: Path):
        path = create_workflow("my_flow", key, directory=tmp_path,
                               endpoints=[endpoint()])
        graph = load_graph(path)
        issues = validate_graph(graph, known_types=known_types())
        assert not has_errors(issues), issues
        assert graph.name == "my_flow"

    def test_agent_template_wires_discovered_endpoint(self, tmp_path: Path):
        path = create_workflow("bot", "agent", directory=tmp_path,
                               endpoints=[endpoint()])
        graph = load_graph(path)
        assert graph.providers["openai_local_8000"]["base_url"] == \
            "http://127.0.0.1:8000/v1"
        assert graph.nodes["think"].config["model"] == "openai_local_8000/test-model"

    def test_agent_template_without_discovery_uses_fallback(self, tmp_path: Path):
        path = create_workflow("bot", "agent", directory=tmp_path, endpoints=[])
        graph = load_graph(path)
        assert graph.nodes["think"].config["model"] == "ollama/llama3.2"
        assert "No inference server was discovered" in path.read_text()

    def test_refuses_overwrite(self, tmp_path: Path):
        create_workflow("dup", "blank", directory=tmp_path)
        with pytest.raises(FileExistsError):
            create_workflow("dup", "blank", directory=tmp_path)
        create_workflow("dup", "blank", directory=tmp_path, force=True)  # ok

    def test_bad_names_rejected(self, tmp_path: Path):
        for bad in ("1abc", "../evil", "has space", ""):
            with pytest.raises(ValueError):
                create_workflow(bad, "blank", directory=tmp_path)

    def test_agent_template_has_a_tool(self, tmp_path: Path):
        path = create_workflow("bot", "agent", directory=tmp_path,
                               endpoints=[endpoint()])
        graph = load_graph(path)
        tools = graph.nodes["think"].config["tools"]
        assert tools and tools[0]["function"] == "graphx.demo:sysinfo"


class TestEvalScaffold:
    EVAL_KEYS = [k for k, t in TEMPLATES.items() if t.eval_body]

    @pytest.mark.parametrize("key", EVAL_KEYS)
    def test_llm_templates_scaffold_a_loadable_eval(self, key, tmp_path: Path):
        from graphx.eval.dataset import load_dataset
        create_workflow("my_flow", key, directory=tmp_path,
                        endpoints=[endpoint()])
        eval_path = tmp_path / "my_flow.eval.yaml"
        assert eval_path.exists()
        dataset = load_dataset(eval_path)
        assert dataset.dataset == "my_flow"
        assert dataset.cases

    def test_blank_scaffolds_no_eval(self, tmp_path: Path):
        create_workflow("my_flow", "blank", directory=tmp_path)
        assert not (tmp_path / "my_flow.eval.yaml").exists()

    def test_existing_eval_kept_without_force(self, tmp_path: Path):
        eval_path = tmp_path / "my_flow.eval.yaml"
        eval_path.write_text("mine")
        create_workflow("my_flow", "agent", directory=tmp_path,
                        endpoints=[endpoint()])
        assert eval_path.read_text() == "mine"
        create_workflow("my_flow", "agent", directory=tmp_path,
                        endpoints=[endpoint()], force=True)
        assert eval_path.read_text() != "mine"


class TestNewCommand:
    def test_new_blank_via_cli(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli_app, ["new", "hello_flow"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "hello_flow.yaml").exists()
        assert "created" in result.output

    def test_new_unknown_template_lists_options(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli_app, ["new", "x", "--template", "nope"])
        assert result.exit_code == 1
        assert "blank" in result.output and "agent" in result.output
        assert "review" in result.output and "watchdog" in result.output

    def test_new_llm_template_mentions_eval_scaffold(self, tmp_path: Path,
                                                     monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli_app, ["new", "bot", "-t", "review"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "bot.eval.yaml").exists()
        assert "bot.eval.yaml" in result.output

    def test_new_duplicate_fails_without_force(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        CliRunner().invoke(cli_app, ["new", "again"])
        result = CliRunner().invoke(cli_app, ["new", "again"])
        assert result.exit_code == 1
        assert "already exists" in result.output
