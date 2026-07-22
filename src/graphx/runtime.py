"""Shared run wiring: build Services for a graph, tear them down after."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from .engine.services import Services
from .llm.client import LLMClient
from .model.graph import Graph
from .secrets import Redactor, SecretResolver, SecretStore


@asynccontextmanager
async def graph_services(graph: Graph, store: SecretStore | None = None):
    resolver = SecretResolver(store or SecretStore())
    redactor = Redactor(resolver.used_values)   # live view of handed-out values
    http = httpx.AsyncClient(timeout=60.0)
    llm = LLMClient(providers=graph.providers, http=http, resolver=resolver)
    mcp = None
    if graph.mcp_servers:
        from .mcp_client.manager import McpManager
        mcp = McpManager(graph.mcp_servers, resolver=resolver)
    services = Services(llm=llm, http=http, mcp=mcp,
                        secrets=resolver, redactor=redactor)
    try:
        yield services
    finally:
        if mcp is not None:
            await mcp.aclose()
        await http.aclose()
