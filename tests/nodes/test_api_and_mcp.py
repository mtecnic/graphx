"""api node via respx; MCP manager against an in-process FastMCP server."""

import httpx
import pytest
import respx

from graphx.engine.services import FakeClock, Services
from graphx.model.graph import RetryPolicy
from graphx.nodes.api import json_path

from conftest import Harness, edge, graph, node


def http_services() -> Services:
    return Services(clock=FakeClock(), http=httpx.AsyncClient())


class TestJsonPath:
    def test_paths(self):
        data = {"a": {"b": [{"c": 7}, {"c": 8}]}}
        assert json_path(data, "$.a.b[1].c") == 8
        assert json_path(data, "$.a.b[0]") == {"c": 7}

    def test_bad_path(self):
        from graphx.nodes.api import ApiError
        with pytest.raises(ApiError):
            json_path({"a": 1}, "$.missing.x")


class TestApiNode:
    @respx.mock
    async def test_get_with_extraction(self):
        respx.get("https://api.test/items").mock(return_value=httpx.Response(
            200, json={"data": {"items": [{"id": 1}, {"id": 2}]}}))
        h = Harness(graph(
            [node("fetch", type="api", url="https://api.test/items",
                  output={"first_id": "$.data.items[0].id"})],
            [edge("fetch", "end")], entry=["fetch"],
        ), services=http_services())
        outcome = await h.run()
        assert outcome.status == "finished"
        output = h.events_of("node_finished")[0].data["output"]
        assert output["first_id"] == 1
        assert output["status"] == 200

    @respx.mock
    async def test_500_retries_then_succeeds(self):
        route = respx.get("https://api.test/flaky")
        route.side_effect = [httpx.Response(500), httpx.Response(200, json={"ok": True})]
        h = Harness(graph(
            [node("fetch", type="api", url="https://api.test/flaky",
                  retry=RetryPolicy(attempts=3, jitter=False))],
            [edge("fetch", "end")], entry=["fetch"],
        ), services=http_services())
        outcome = await h.run()
        assert outcome.status == "finished"
        assert len(h.events_of("node_retrying")) == 1

    @respx.mock
    async def test_404_is_deterministic(self):
        respx.get("https://api.test/gone").mock(return_value=httpx.Response(404))
        h = Harness(graph(
            [node("fetch", type="api", url="https://api.test/gone",
                  retry=RetryPolicy(attempts=5))],
            [edge("fetch", "end")], entry=["fetch"],
        ), services=http_services())
        outcome = await h.run()
        assert outcome.status == "failed"
        assert h.events_of("node_retrying") == []

    @respx.mock
    async def test_expect_status_allows_404(self):
        respx.get("https://api.test/maybe").mock(return_value=httpx.Response(
            404, json={"error": "not found"}))
        h = Harness(graph(
            [node("fetch", type="api", url="https://api.test/maybe",
                  expect_status=[200, 404])],
            [edge("fetch", "end")], entry=["fetch"],
        ), services=http_services())
        outcome = await h.run()
        assert outcome.status == "finished"


mcp_sdk = pytest.importorskip("mcp")


class TestMcp:
    async def _manager_with_inprocess_server(self):
        """FastMCP server connected over memory streams, injected into McpManager."""
        from mcp.server.fastmcp import FastMCP
        from mcp.shared.memory import create_connected_server_and_client_session

        server = FastMCP("test-server")

        @server.tool()
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        from graphx.mcp_client.manager import McpManager
        manager = McpManager({"calc": {"transport": "stdio", "command": ["unused"]}})
        cm = create_connected_server_and_client_session(server._mcp_server)
        session = await manager._stack.enter_async_context(cm)
        manager._sessions["calc"] = session
        return manager

    async def test_list_and_call_tool(self):
        manager = await self._manager_with_inprocess_server()
        try:
            tools = await manager.list_tools("calc")
            assert "add" in tools
            result = await manager.call("calc", "add", {"a": 2, "b": 3})
            assert result["text"] == "5"
        finally:
            await manager.aclose()

    async def test_mcp_node_through_engine(self):
        manager = await self._manager_with_inprocess_server()
        try:
            services = Services(clock=FakeClock(), mcp=manager)
            h = Harness(graph(
                [node("calc", type="mcp", server="calc", tool="add",
                      args={"a": 20, "b": 22})],
                [edge("calc", "end")], entry=["calc"],
            ), services=services)
            outcome = await h.run()
            assert outcome.status == "finished"
            assert h.events_of("node_finished")[0].data["output"]["text"] == "42"
        finally:
            await manager.aclose()
