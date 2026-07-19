"""Shared run wiring: build Services for a graph, tear them down after."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .engine.services import Services
from .llm.client import LLMClient
from .model.graph import Graph


@asynccontextmanager
async def graph_services(graph: Graph):
    http = httpx.AsyncClient(timeout=60.0)
    llm = LLMClient(providers=graph.providers, http=http)
    mcp = None
    if graph.mcp_servers:
        from .mcp_client.manager import McpManager
        mcp = McpManager(graph.mcp_servers)
    services = Services(llm=llm, http=http, mcp=mcp)
    try:
        yield services
    finally:
        if mcp is not None:
            await mcp.aclose()
        await http.aclose()
