"""CLI: graphx connectors / graphx add."""

import pytest
from typer.testing import CliRunner

from graphx.cli import app
from graphx.model.yaml_loader import load_graph
from graphx.nodes.registry import load_builtin_nodes

load_builtin_nodes()
runner = CliRunner()

BASE_WF = ("version: 1\nname: t\nentry: [start]\n"
           'nodes:\n  - {id: start, type: shell, command: ["echo", "hi"]}\n'
           "edges:\n  - {from: start, to: end}\n")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    monkeypatch.setattr("graphx.secrets._keyring", lambda: None)


class TestConnectorsCommand:
    def test_lists_all_categories(self):
        result = runner.invoke(app, ["connectors"])
        assert result.exit_code == 0
        for key in ("slack", "sendgrid", "github_issue", "postgres_query"):
            assert key in result.output

    def test_category_filter(self):
        result = runner.invoke(app, ["connectors", "--category", "dev"])
        assert "github_issue" in result.output
        assert "slack" not in result.output

    def test_unknown_category(self):
        assert runner.invoke(app, ["connectors", "--category", "nope"]).exit_code == 1


class TestAddCommand:
    def test_add_slack(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(BASE_WF)
        result = runner.invoke(app, ["add", "slack", str(wf),
                                     "message=deploy done", "--id", "notify"])
        assert result.exit_code == 0, result.output
        assert "graphx secret set slack_webhook_url" in result.output
        graph = load_graph(wf)
        assert graph.nodes["notify"].config["url"] == "secret://slack_webhook_url"

    def test_add_gmail_wires_mcp_server_and_note(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(BASE_WF)
        result = runner.invoke(app, ["add", "gmail", str(wf), "--id", "inbox"])
        assert result.exit_code == 0, result.output
        assert "OAuth" in result.output
        graph = load_graph(wf)
        assert "gmail" in graph.mcp_servers

    def test_missing_required_field(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(BASE_WF)
        result = runner.invoke(app, ["add", "github_issue", str(wf), "owner=o"])
        assert result.exit_code == 1
        assert "missing required field" in result.output

    def test_unknown_connector_lists_options(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(BASE_WF)
        result = runner.invoke(app, ["add", "nope", str(wf)])
        assert result.exit_code == 1
        assert "slack" in result.output

    def test_postgres_extra_hint(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(BASE_WF)
        result = runner.invoke(app, ["add", "postgres_query", str(wf),
                                     "query=SELECT 1"])
        assert result.exit_code == 0, result.output
        collapsed = "".join(result.output.split())   # undo rich line-wrapping
        assert "graphx[postgres]" in collapsed
