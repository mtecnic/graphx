"""api node: call an external HTTP API, extract fields with $.dot.paths."""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..engine.errors import GraphxError, TransientError
from .registry import NodeContext, NodeResult, node_type

_PATH_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


class ApiError(GraphxError):
    pass


class ApiConfig(BaseModel):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None
    body: str | None = None
    output: dict[str, str] = Field(default_factory=dict)   # field -> "$.data.items[0].id"
    expect_status: list[int] | None = None


def json_path(data: Any, path: str) -> Any:
    if not path.startswith("$"):
        raise ApiError(f"output path must start with '$': {path!r}")
    rest = path[1:]
    pos = 0
    current = data
    while pos < len(rest):
        match = _PATH_TOKEN.match(rest, pos)
        if match is None:
            raise ApiError(f"bad path segment in {path!r} at '{rest[pos:]}'")
        key, index = match.group(1), match.group(2)
        try:
            current = current[key] if key is not None else current[int(index)]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApiError(f"{path}: cannot resolve "
                           f"'{key or index}' in {type(current).__name__}") from exc
        pos = match.end()
    return current


@node_type("api", config_model=ApiConfig)
async def api_node(ctx: NodeContext) -> NodeResult:
    config: ApiConfig = ctx.config  # type: ignore[assignment]
    client: httpx.AsyncClient | None = ctx.services.http
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        # resolve secret:// refs only now, at the request boundary
        resolve = ctx.services.secrets.resolve
        try:
            response = await client.request(
                config.method.upper(), resolve(config.url),
                headers=resolve(config.headers) or None,
                params=resolve(config.params) or None,
                json=resolve(config.json_body),
                content=resolve(config.body))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"HTTP request failed: {exc}") from exc

        ok_statuses = config.expect_status or range(200, 300)
        if response.status_code not in ok_statuses:
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = None
                raw = response.headers.get("retry-after")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        retry_after = None
                raise TransientError(f"HTTP {response.status_code}: {response.text[:300]}",
                                     retry_after=retry_after)
            raise ApiError(f"HTTP {response.status_code}: {response.text[:500]}")

        try:
            data = response.json()
        except ValueError:
            data = None
        output: dict[str, Any] = {
            "status": response.status_code,
            "json": data,
            "text": response.text[:64 * 1024],
        }
        for field, path in config.output.items():
            output[field] = json_path(data, path)
        return NodeResult(output=output)
    finally:
        if own_client:
            await client.aclose()
