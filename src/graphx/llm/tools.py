"""Agent tool plumbing: function tools and MCP tools behind one interface."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..engine.errors import ConfigError
from ..nodes.function import load_callable
from .client import ToolDef

GENERIC_PARAMS = {"type": "object", "properties": {}, "additionalProperties": True}


@dataclass
class ResolvedTool:
    definition: ToolDef
    call: Callable[[dict[str, Any]], Awaitable[Any]]


async def resolve_tools(specs: list[dict[str, Any]], services) -> list[ResolvedTool]:
    tools: list[ResolvedTool] = []
    for spec in specs or []:
        if "function" in spec:
            fn = load_callable(spec["function"])
            name = spec.get("name") or spec["function"].rsplit(":", 1)[-1]
            definition = ToolDef(name=name, description=spec.get("description", ""),
                                 parameters=spec.get("parameters") or GENERIC_PARAMS)

            async def call(args: dict[str, Any], _fn=fn) -> Any:
                import asyncio
                if asyncio.iscoroutinefunction(_fn):
                    return await _fn(**args)
                return await asyncio.to_thread(_fn, **args)

            tools.append(ResolvedTool(definition, call))
        elif "mcp" in spec:
            if services.mcp is None:
                raise ConfigError("workflow uses MCP tools but no mcp_servers are configured")
            server = spec["mcp"]
            tool_name = spec.get("tool")
            if not tool_name:
                raise ConfigError(f"MCP tool spec needs 'tool': {spec!r}")
            definition = await services.mcp.tool_def(server, tool_name)
            if spec.get("name"):
                definition = ToolDef(name=spec["name"], description=definition.description,
                                     parameters=definition.parameters)

            async def call(args: dict[str, Any], _server=server, _tool=tool_name) -> Any:
                return await services.mcp.call(_server, _tool, args)

            tools.append(ResolvedTool(definition, call))
        else:
            raise ConfigError(f"tool spec needs 'function' or 'mcp': {spec!r}")
    return tools


def tool_result_to_text(result: Any, limit: int = 8000) -> str:
    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, default=str)
    return text[:limit]
