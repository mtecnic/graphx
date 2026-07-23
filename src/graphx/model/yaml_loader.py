"""YAML → Graph.

The YAML file is the public interface of graphx. This loader is
deliberately tolerant about node config (each node type validates its
own config against its pydantic model at registry level) and strict
about structure (edges, entry, state, policies).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .graph import (
    END, Budget, EdgeSpec, Graph, GraphConfig, NodeSpec,
    ResiliencePolicy, RetryPolicy, StaticFallback,
)
from .state import REDUCERS, ChannelSpec


class LoadError(ValueError):
    pass


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_DURATION_FACTORS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}

# Node-level keys consumed by the loader; everything else is node-type config.
_RESERVED_NODE_KEYS = {
    "id", "type", "retry", "timeout", "fallbacks", "validation", "cache",
    "budget", "on_error", "updates", "max_iterations", "on_exhausted",
}


def parse_duration(value: Any, where: str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise LoadError(f"{where}: bad duration {value!r} (use e.g. 500ms, 20s, 15m, 1h)")
    return float(match.group(1)) * _DURATION_FACTORS[match.group(2)]


def _interpolate_env(value: Any, where: str) -> Any:
    # An unset ${VAR} is left in place (not a load error), so a well-formed
    # workflow still validates / opens / can be edited without every env var
    # exported — mirroring secret:// laziness. A set var interpolates as before;
    # an unresolved placeholder simply surfaces when the node runs.
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            var = m.group(1)
            return os.environ.get(var, m.group(0))
        return _ENV_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v, where) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v, where) for v in value]
    return value


def _parse_retry(raw: Any, where: str) -> RetryPolicy:
    if raw is None:
        return RetryPolicy()
    if not isinstance(raw, dict):
        raise LoadError(f"{where}: retry must be a mapping")
    kwargs: dict[str, Any] = {}
    for key in ("attempts", "factor", "jitter", "retry_on"):
        if key in raw:
            kwargs[key] = raw[key]
    for key in ("base", "max_delay"):
        if key in raw:
            kwargs[key] = parse_duration(raw[key], f"{where}.retry.{key}")
    return RetryPolicy(**kwargs)


def _parse_budget(raw: Any, where: str) -> Budget | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LoadError(f"{where}: budget must be a mapping")
    deadline = raw.get("deadline")
    return Budget(
        tokens=raw.get("tokens"),
        cost_usd=raw.get("cost_usd"),
        deadline_s=parse_duration(deadline, f"{where}.budget.deadline") if deadline else None,
    )


def _parse_fallbacks(raw: Any, where: str) -> tuple[str | StaticFallback, ...]:
    if raw is None:
        return ()
    out: list[str | StaticFallback] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and "static" in item:
            out.append(StaticFallback(output=dict(item["static"])))
        else:
            raise LoadError(f"{where}.fallbacks[{i}]: expected model name or {{static: {{...}}}}")
    return tuple(out)


def _parse_node(raw: dict[str, Any]) -> tuple[NodeSpec, list[EdgeSpec]]:
    node_id = raw.get("id")
    if not node_id or not isinstance(node_id, str):
        raise LoadError(f"node missing string 'id': {raw!r}")
    where = f"node '{node_id}'"
    node_type = raw.get("type")
    if not node_type:
        raise LoadError(f"{where}: missing 'type'")

    timeout = raw.get("timeout")
    validation = raw.get("validation") or {}
    resilience = ResiliencePolicy(
        retry=_parse_retry(raw.get("retry"), where),
        timeout_s=parse_duration(timeout, f"{where}.timeout") if timeout is not None else None,
        fallbacks=_parse_fallbacks(raw.get("fallbacks"), where),
        validation_reasks=int(validation.get("reasks", 2)),
        cache=bool(raw.get("cache", False)),
        budget=_parse_budget(raw.get("budget"), where),
    )
    config = {k: v for k, v in raw.items() if k not in _RESERVED_NODE_KEYS}

    edges: list[EdgeSpec] = []
    if raw.get("on_error"):
        edges.append(EdgeSpec(source=node_id, target=str(raw["on_error"]), kind="on_error"))

    spec = NodeSpec(
        id=node_id,
        type=str(node_type),
        config=config,
        resilience=resilience,
        updates=dict(raw.get("updates") or {}),
        max_iterations=raw.get("max_iterations"),
        on_exhausted=raw.get("on_exhausted"),
    )
    return spec, edges


def load_raw(path: str | Path) -> dict[str, Any]:
    yaml = YAML(typ="safe", pure=True)
    with open(path) as f:
        data = yaml.load(f)
    if not isinstance(data, dict):
        raise LoadError(f"{path}: top level must be a mapping")
    return data


def load_graph(path: str | Path) -> Graph:
    data = load_raw(path)
    return build_graph(data, source_path=str(path))


def build_graph(data: dict[str, Any], source_path: str | None = None) -> Graph:
    version = data.get("version")
    if version != 1:
        raise LoadError(f"unsupported or missing 'version' (expected 1, got {version!r})")
    name = data.get("name")
    if not name:
        raise LoadError("missing 'name'")

    data = _interpolate_env(data, name)

    channels: dict[str, ChannelSpec] = {}
    for chan_name, raw in (data.get("state") or {}).items():
        raw = raw or {}
        reducer = raw.get("reducer", "last")
        if reducer not in REDUCERS:
            raise LoadError(f"state.{chan_name}: unknown reducer '{reducer}' "
                            f"(have: {', '.join(REDUCERS)})")
        channels[chan_name] = ChannelSpec(
            name=chan_name, type_=raw.get("type", "any"),
            reducer=reducer, default=raw.get("default"),
        )

    nodes: dict[str, NodeSpec] = {}
    edges: list[EdgeSpec] = []
    for raw_node in data.get("nodes") or []:
        spec, extra_edges = _parse_node(dict(raw_node))
        if spec.id in nodes:
            raise LoadError(f"duplicate node id '{spec.id}'")
        if spec.id == END:
            raise LoadError(f"'{END}' is a reserved node id")
        nodes[spec.id] = spec
        edges.extend(extra_edges)

    for i, raw_edge in enumerate(data.get("edges") or []):
        if not isinstance(raw_edge, dict) or "from" not in raw_edge or "to" not in raw_edge:
            raise LoadError(f"edges[{i}]: expected {{from: ..., to: ...}}")
        edges.append(EdgeSpec(
            source=str(raw_edge["from"]), target=str(raw_edge["to"]),
            when=raw_edge.get("when"), kind=raw_edge.get("kind", "normal"),
        ))

    entry_raw = data.get("entry")
    if not entry_raw:
        raise LoadError("missing 'entry'")
    entry = tuple([entry_raw] if isinstance(entry_raw, str) else [str(e) for e in entry_raw])

    raw_config = data.get("config") or {}
    config = GraphConfig(
        max_steps=int(raw_config.get("max_steps", 25)),
        max_concurrency=int(raw_config.get("max_concurrency", 8)),
        budget=_parse_budget(raw_config.get("budget"), "config"),
    )

    return Graph(
        name=str(name),
        channels=channels,
        nodes=nodes,
        edges=tuple(edges),
        entry=entry,
        config=config,
        mcp_servers=dict(data.get("mcp_servers") or {}),
        providers=dict(data.get("providers") or {}),
        description=str(data.get("description") or ""),
        source_path=source_path,
    )
