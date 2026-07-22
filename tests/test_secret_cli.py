"""CLI: secret set/list/rm and the missing-secret pre-run guard."""

import pytest
from typer.testing import CliRunner

from graphx.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    monkeypatch.setattr("graphx.secrets._keyring", lambda: None)


class TestSecretCommands:
    def test_set_and_list_never_shows_value(self):
        result = runner.invoke(app, ["secret", "set", "mykey", "--value", "sk-XYZ"])
        assert result.exit_code == 0
        assert "sk-XYZ" not in result.output

        listing = runner.invoke(app, ["secret", "list"])
        assert listing.exit_code == 0
        assert "mykey" in listing.output
        assert "sk-XYZ" not in listing.output

    def test_set_via_stdin(self):
        result = runner.invoke(app, ["secret", "set", "piped", "--stdin"], input="sk-PIPED\n")
        assert result.exit_code == 0
        listing = runner.invoke(app, ["secret", "list"])
        assert "piped" in listing.output
        assert "sk-PIPED" not in listing.output

    def test_rm(self):
        runner.invoke(app, ["secret", "set", "gone", "--value", "x"])
        assert runner.invoke(app, ["secret", "rm", "gone"]).exit_code == 0
        assert runner.invoke(app, ["secret", "rm", "gone"]).exit_code == 1

    def test_empty_list(self):
        result = runner.invoke(app, ["secret", "list"])
        assert "no stored secrets" in result.output


WF = """\
version: 1
name: needs_secret
entry: [call]
nodes:
  - id: call
    type: api
    method: GET
    url: "https://api.test/x"
    headers: { Authorization: "Bearer secret://api_token" }
edges:
  - { from: call, to: end }
"""


class TestMissingSecretGuard:
    def test_run_refuses_when_secret_unset(self, tmp_path):
        wf = tmp_path / "wf.yaml"
        wf.write_text(WF)
        result = runner.invoke(app, ["run", str(wf)])
        assert result.exit_code == 1
        assert "missing secret" in result.output
        assert "api_token" in result.output
        assert "graphx secret set api_token" in result.output

    def test_validate_still_opens_with_unset_secret(self, tmp_path):
        # secret:// is lazy, so validation must NOT fail on an unset secret
        wf = tmp_path / "wf.yaml"
        wf.write_text(WF)
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == 0
