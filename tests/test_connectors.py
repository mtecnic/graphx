"""Connector registry: render validity, secret wiring, CLI, live request shape."""


import httpx
import pytest
import respx

from graphx.connectors.registry import all_connectors, get
from graphx.nodes.registry import get_node_type, load_builtin_nodes
from graphx.secrets import find_secret_refs

load_builtin_nodes()

SAMPLE = {
    "message": "hi", "chat_id": "123", "url": "https://x/h", "auth": True,
    "to": "a@b.com", "from_email": "f@b.com", "subject": "s", "body": "b",
    "host": "smtp.x", "username": "u", "from_addr": "f@x", "to_addr": "t@x",
    "owner": "o", "repo": "r", "title": "t", "issue_number": "5", "comment": "c",
    "project": "12", "query": "SELECT 1", "bucket": "bk", "key": "k/o",
}


class TestRegistry:
    @pytest.mark.parametrize("conn", all_connectors(), ids=lambda c: c.key)
    def test_render_produces_valid_node(self, conn):
        result = conn.render(f"{conn.key}_n", dict(SAMPLE))
        node_type = get_node_type(result.node["type"])
        cfg = {k: v for k, v in result.node.items() if k not in ("id", "type")}
        node_type.config_model.model_validate(cfg)   # raises on invalid config

    @pytest.mark.parametrize("conn", all_connectors(), ids=lambda c: c.key)
    def test_declared_secrets_appear_in_node(self, conn):
        result = conn.render(f"{conn.key}_n", dict(SAMPLE))
        refs = find_secret_refs(result.node)
        for name in result.secret_names:
            assert name in refs, f"{conn.key}: {name} declared but not referenced"

    def test_missing_field_detection(self):
        conn = get("github_issue")
        assert set(conn.missing_fields({})) >= {"owner", "repo", "title"}
        assert conn.missing_fields(
            {"owner": "o", "repo": "r", "title": "t"}) == []

    def test_optional_field_default(self):
        # webhook.method defaults to POST
        result = get("webhook").render("w", {"url": "https://x", "message": "m"})
        assert result.node["method"] == "POST"


class TestGoldenShapes:
    def test_slack(self):
        r = get("slack").render("n", {"message": "deploy done"})
        assert r.node["url"] == "secret://slack_webhook_url"
        assert r.node["json_body"] == {"text": "deploy done"}

    def test_telegram_token_in_path(self):
        r = get("telegram").render("n", {"chat_id": "42", "message": "hi"})
        assert r.node["url"] == \
            "https://api.telegram.org/botsecret://telegram_token/sendMessage"
        assert r.node["json_body"]["chat_id"] == "42"

    def test_sendgrid_content_ordering_and_shape(self):
        r = get("sendgrid").render("n", {
            "to": "x@y.com", "from_email": "me@d.com", "subject": "s", "body": "b"})
        body = r.node["json_body"]
        assert body["personalizations"][0]["to"][0]["email"] == "x@y.com"
        assert body["from"] == {"email": "me@d.com"}
        assert body["content"][0]["type"] == "text/plain"   # plain first
        assert r.node["headers"]["Authorization"] == "Bearer secret://sendgrid_key"

    def test_github_headers_present(self):
        r = get("github_issue").render("n", {
            "owner": "octo", "repo": "hello", "title": "t"})
        h = r.node["headers"]
        assert h["Accept"] == "application/vnd.github+json"
        assert h["User-Agent"] == "graphx"                  # GitHub requires it
        assert h["Authorization"] == "Bearer secret://github_token"
        assert r.node["url"] == "https://api.github.com/repos/octo/hello/issues"

    def test_gitlab_private_token_header(self):
        r = get("gitlab_issue").render("n", {"project": "278964", "title": "t"})
        assert r.node["headers"]["PRIVATE-TOKEN"] == "secret://gitlab_token"
        assert r.node["json_body"]["description"] == ""

    def test_gmail_wires_mcp_server_not_secret(self):
        r = get("gmail").render("n", {})
        assert r.secret_names == []
        assert "gmail" in r.mcp_servers
        assert "npx" in r.mcp_servers["gmail"]["command"]
        assert "OAuth" in r.note

    def test_smtp_function_shape(self):
        r = get("smtp").render("n", {
            "host": "smtp.x", "username": "u", "from_addr": "f@x",
            "to_addr": "t@x", "subject": "s", "body": "b"})
        assert r.node["handler"] == "graphx.connectors.helpers:smtp_send"
        assert r.node["args"]["password"] == "secret://smtp_password"
        assert r.node["args"]["port"] == 587


class TestLiveRequest:
    @respx.mock
    async def test_github_connector_sends_real_token_redacted_in_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
        monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
        from graphx.engine.events import EventBus
        from graphx.engine.executor import Executor
        from graphx.engine.services import Services
        from graphx.model.yaml_writer import WorkflowFile
        from graphx.persistence.db import open_db
        from graphx.persistence.sqlite_checkpointer import SqliteCheckpointer
        from graphx.secrets import Redactor, SecretResolver, SecretStore

        SECRET = "ghp_TOPSECRET_TOKEN_123"
        store = SecretStore()
        store.set("github_token", SECRET)

        # build a workflow by adding the connector, then load + run it
        wf_path = tmp_path / "wf.yaml"
        wf_path.write_text(
            "version: 1\nname: gh\nentry: [issue]\nnodes: []\nedges: "
            "[{from: issue, to: end}]\n")
        wf = WorkflowFile(wf_path)
        result = get("github_issue").render("issue", {
            "owner": "octo", "repo": "hello", "title": "hi", "body": "b"})
        wf.add_node(result.node)
        wf.save()

        route = respx.post("https://api.github.com/repos/octo/hello/issues").mock(
            return_value=httpx.Response(201, json={"number": 1}))

        from graphx.model.yaml_loader import load_graph
        graph = load_graph(wf_path)
        resolver = SecretResolver(store)
        services = Services(http=httpx.AsyncClient(),
                            secrets=resolver, redactor=Redactor(resolver.used_values))
        db = await open_db(tmp_path / "run.db")
        bus = EventBus(run_id="r", thread_id="t", sink=SqliteCheckpointer(db).event_sink)
        try:
            outcome = await Executor(graph, SqliteCheckpointer(db), bus, services).run("t", {})
        finally:
            bus.close()
            await db.close()
            await services.http.aclose()

        assert outcome.status == "finished"
        # (a) real token went out on the wire with the GitHub headers
        req = route.calls[0].request
        assert req.headers["authorization"] == f"Bearer {SECRET}"
        assert req.headers["accept"] == "application/vnd.github+json"
        assert req.headers["user-agent"] == "graphx"
        # (b) the secret is nowhere in the persisted DB
        assert SECRET.encode() not in (tmp_path / "run.db").read_bytes()
