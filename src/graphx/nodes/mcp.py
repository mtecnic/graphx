"""mcp node: call one tool on a configured MCP server."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..engine.errors import ConfigError
from .registry import NodeContext, NodeResult, node_type


class McpConfig(BaseModel):
    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


@node_type("mcp", config_model=McpConfig)
async def mcp_node(ctx: NodeContext) -> NodeResult:
    config: McpConfig = ctx.config  # type: ignore[assignment]
    if ctx.services.mcp is None:
        raise ConfigError("no MCP manager configured (does the workflow define mcp_servers?)")
    result = await ctx.services.mcp.call(config.server, config.tool, config.args)
    return NodeResult(output=result)
