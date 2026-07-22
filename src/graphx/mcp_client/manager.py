"""MCP server session pool.

Sessions connect lazily on first use and live for the whole run
(AsyncExitStack teardown in aclose()). A crashed/broken server surfaces
as TransientError, so per-node retry policies respawn it transparently.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

from ..engine.errors import ConfigError, TransientError
from ..llm.client import ToolDef


class McpManager:
    def __init__(self, servers: Mapping[str, Any], resolver: Any = None):
        self.configs = dict(servers)
        self._resolver = resolver          # SecretResolver, or None (no resolution)
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._tool_cache: dict[str, dict[str, ToolDef]] = {}

    async def _connect(self, name: str):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ConfigError(
                "MCP support requires the 'mcp' extra: pip install graphx[mcp]") from exc

        config = self.configs.get(name)
        if config is None:
            raise ConfigError(f"unknown MCP server '{name}' "
                              f"(have: {', '.join(sorted(self.configs)) or 'none'})")
        transport = config.get("transport", "stdio")
        if transport != "stdio":
            raise ConfigError(f"MCP server '{name}': only stdio transport is "
                              f"supported for now, got '{transport}'")
        command = config.get("command")
        if not command:
            raise ConfigError(f"MCP server '{name}': missing 'command'")

        env = config.get("env") or None
        if env and self._resolver is not None:
            env = self._resolver.resolve(env)   # resolve secret:// at spawn
        params = StdioServerParameters(
            command=command[0], args=list(command[1:]),
            env=env, cwd=config.get("cwd"))
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception as exc:  # noqa: BLE001 — startup failures are retryable
            raise TransientError(f"MCP server '{name}' failed to start: {exc}") from exc
        self._sessions[name] = session
        return session

    async def _session(self, name: str):
        session = self._sessions.get(name)
        if session is None:
            session = await self._connect(name)
        return session

    def _drop(self, name: str) -> None:
        self._sessions.pop(name, None)
        self._tool_cache.pop(name, None)

    async def list_tools(self, server: str) -> dict[str, ToolDef]:
        if server in self._tool_cache:
            return self._tool_cache[server]
        session = await self._session(server)
        try:
            result = await session.list_tools()
        except Exception as exc:  # noqa: BLE001 — session likely dead; retry respawns
            self._drop(server)
            raise TransientError(f"MCP server '{server}' list_tools failed: {exc}") from exc
        tools = {t.name: ToolDef(name=t.name, description=t.description or "",
                                 parameters=t.inputSchema or {"type": "object",
                                                              "properties": {}})
                 for t in result.tools}
        self._tool_cache[server] = tools
        return tools

    async def tool_def(self, server: str, tool: str) -> ToolDef:
        tools = await self.list_tools(server)
        if tool not in tools:
            raise ConfigError(f"MCP server '{server}' has no tool '{tool}' "
                              f"(have: {', '.join(sorted(tools))})")
        return tools[tool]

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        session = await self._session(server)
        try:
            result = await session.call_tool(tool, args or {})
        except Exception as exc:  # noqa: BLE001 — session likely dead; retry respawns
            self._drop(server)
            raise TransientError(f"MCP call {server}.{tool} failed: {exc}") from exc

        parts: list[Any] = []
        for block in result.content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                parts.append(block.text)
            else:
                parts.append(str(block))
        text = "\n".join(str(p) for p in parts)
        if getattr(result, "isError", False):
            # tool-level errors are deterministic (bad args, not a dead server)
            raise ConfigError(f"MCP tool {server}.{tool} returned an error: {text[:300]}")
        return {"text": text}

    async def aclose(self) -> None:
        self._sessions.clear()
        self._tool_cache.clear()
        await self._stack.aclose()
