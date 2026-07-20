"""OpenAPI introspection: turn a live service spec into api-node config.

Given a base URL (or a direct spec URL), fetch the OpenAPI 3.x spec,
list its operations, and scaffold an `api` node for a chosen one:
method, templated url (`{id}` → `<state.id>`), required params, a
json_body skeleton, and suggested `output:` extraction paths derived
from the 2xx response schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..engine.errors import GraphxError

SPEC_SUFFIXES = (".json", ".yaml", ".yml")
SPEC_CANDIDATES = ("openapi.json", "openapi.yaml", "swagger.json",
                   "api/openapi.json", "v1/openapi.json")
_MAX_REF_DEPTH = 8
_MAX_OUTPUT_PATHS = 8
_MAX_OUTPUT_DEPTH = 4


class SpecError(GraphxError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    location: str          # path | query | header
    required: bool
    type_: str = "string"
    description: str = ""


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    summary: str = ""
    operation_id: str = ""
    params: tuple[Param, ...] = ()
    request_body: dict[str, Any] | None = None     # dereferenced JSON schema
    response: dict[str, Any] | None = None         # dereferenced 2xx JSON schema

    @property
    def label(self) -> str:
        text = f"{self.method.upper()} {self.path}"
        return f"{text} — {self.summary}" if self.summary else text


# ------------------------------------------------------------------ fetch

async def fetch_spec(url: str, http: httpx.AsyncClient | None = None
                     ) -> tuple[dict[str, Any], str]:
    """Fetch a spec; returns (spec, base_url_for_requests)."""
    own = http is None
    client = http or httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    try:
        url = url.rstrip("/") if not url.endswith(SPEC_SUFFIXES) else url
        candidates = [url] if url.endswith(SPEC_SUFFIXES) else \
            [f"{url}/{suffix}" for suffix in SPEC_CANDIDATES]
        last_error = "no candidates tried"
        for candidate in candidates:
            try:
                response = await client.get(candidate)
            except httpx.HTTPError as exc:
                last_error = f"{candidate}: {exc}"
                continue
            if response.status_code != 200:
                last_error = f"{candidate}: HTTP {response.status_code}"
                continue
            spec = _parse_spec_body(candidate, response)
            if spec is None:
                last_error = f"{candidate}: not a JSON/YAML OpenAPI document"
                continue
            if "paths" not in spec:
                last_error = f"{candidate}: no 'paths' section"
                continue
            return spec, _base_url(candidate, spec)
        raise SpecError(f"could not fetch an OpenAPI spec from {url} ({last_error})")
    finally:
        if own:
            await client.aclose()


def _parse_spec_body(url: str, response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
        return body if isinstance(body, dict) else None
    except ValueError:
        pass
    if url.endswith((".yaml", ".yml")) or "yaml" in response.headers.get("content-type", ""):
        try:
            from ruamel.yaml import YAML
            body = YAML(typ="safe", pure=True).load(response.text)
            return body if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001 — treated as "not a spec"
            return None
    return None


def _base_url(spec_url: str, spec: dict[str, Any]) -> str:
    """Combine where we found the spec with the spec's servers[0].url."""
    parts = urlsplit(spec_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    server = ""
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict):
        server = str(servers[0].get("url") or "")
    if server.startswith(("http://", "https://")):
        return server.rstrip("/")
    if server:
        return origin + "/" + server.strip("/")
    return origin


# ------------------------------------------------------------------ parse

def _deref(schema: Any, root: dict[str, Any], depth: int = 0) -> Any:
    if depth > _MAX_REF_DEPTH or not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = root
        for part in ref[2:].split("/"):
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        return _deref(target, root, depth + 1)
    return schema


def parse_operations(spec: dict[str, Any]) -> list[Operation]:
    operations: list[Operation] = []
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters") or []
        for method in ("get", "post", "put", "patch", "delete", "head"):
            raw = path_item.get(method)
            if not isinstance(raw, dict):
                continue
            params: list[Param] = []
            for raw_param in [*shared_params, *(raw.get("parameters") or [])]:
                raw_param = _deref(raw_param, spec)
                if not isinstance(raw_param, dict) or "name" not in raw_param:
                    continue
                schema = _deref(raw_param.get("schema") or {}, spec)
                params.append(Param(
                    name=str(raw_param["name"]),
                    location=str(raw_param.get("in", "query")),
                    required=bool(raw_param.get("required", False)),
                    type_=str(schema.get("type", "string")),
                    description=str(raw_param.get("description") or ""),
                ))

            request_body = None
            body = _deref(raw.get("requestBody") or {}, spec)
            content = (body.get("content") or {}) if isinstance(body, dict) else {}
            for content_type, media in content.items():
                if "json" in content_type:
                    request_body = _deref((media or {}).get("schema") or {}, spec)
                    break

            response_schema = None
            for status, response in sorted((raw.get("responses") or {}).items()):
                if not str(status).startswith("2"):
                    continue
                response = _deref(response, spec)
                content = (response.get("content") or {}) if isinstance(response, dict) else {}
                for content_type, media in content.items():
                    if "json" in content_type:
                        response_schema = _deref((media or {}).get("schema") or {}, spec)
                        break
                if response_schema is not None:
                    break

            operations.append(Operation(
                method=method, path=str(path),
                summary=str(raw.get("summary") or ""),
                operation_id=str(raw.get("operationId") or ""),
                params=tuple(params),
                request_body=request_body, response=response_schema,
            ))
    return operations


# --------------------------------------------------------------- scaffold

def suggest_outputs(schema: dict[str, Any] | None, root: dict[str, Any] | None = None,
                    ) -> dict[str, str]:
    """Leaf properties of the response schema → {field: "$.path"} suggestions."""
    if not isinstance(schema, dict):
        return {}
    root = root or {}
    out: dict[str, str] = {}

    def walk(node: Any, prefix: str, name: str, depth: int) -> None:
        if len(out) >= _MAX_OUTPUT_PATHS or depth > _MAX_OUTPUT_DEPTH:
            return
        node = _deref(node, root)
        if not isinstance(node, dict):
            return
        node_type = node.get("type") or ("object" if "properties" in node else None)
        if node_type == "object" and isinstance(node.get("properties"), dict):
            for prop, sub in node["properties"].items():
                walk(sub, f"{prefix}.{prop}", str(prop), depth + 1)
        elif node_type == "array":
            walk(node.get("items") or {}, f"{prefix}[0]", name, depth + 1)
        elif prefix != "$":
            field_name = name if name not in out else prefix[2:].replace(".", "_") \
                .replace("[0]", "0")
            if field_name and field_name not in out:
                out[field_name] = prefix

    walk(schema, "$", "", 0)
    return out


def body_skeleton(schema: dict[str, Any] | None, root: dict[str, Any] | None = None
                  ) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    schema = _deref(schema, root or {})
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = set(schema.get("required") or [])
    skeleton: dict[str, Any] = {}
    for name in properties:
        # required fields always scaffolded; optional ones only for small bodies
        if name in required or len(properties) <= 4:
            skeleton[name] = f"<state.{name}>"
    return skeleton


def scaffold_api_node(op: Operation, base_url: str, spec: dict[str, Any],
                      node_id: str | None = None) -> dict[str, Any]:
    """Build an api-node config dict ready for yaml_writer.add_node()."""
    url = base_url.rstrip("/") + op.path
    for param in op.params:
        if param.location == "path":
            url = url.replace("{" + param.name + "}", f"<state.{param.name}>")

    node: dict[str, Any] = {
        "id": node_id or (op.operation_id or
                          f"{op.method}_{op.path.strip('/').replace('/', '_') or 'root'}"
                          ).replace("{", "").replace("}", ""),
        "type": "api",
        "method": op.method.upper(),
        "url": url,
    }
    query = {p.name: f"<state.{p.name}>" for p in op.params
             if p.location == "query" and p.required}
    if query:
        node["params"] = query
    headers = {p.name: f"<state.{p.name}>" for p in op.params
               if p.location == "header" and p.required}
    if headers:
        node["headers"] = headers
    if op.method in ("post", "put", "patch"):
        skeleton = body_skeleton(op.request_body, spec)
        if skeleton is not None:
            node["json_body"] = skeleton
    outputs = suggest_outputs(op.response, spec)
    if outputs:
        node["output"] = outputs
    return node
