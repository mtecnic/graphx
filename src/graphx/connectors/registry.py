"""Service connector model: credential-wired, drop-in node presets.

A connector renders a ready-to-use node (optionally with an mcp_servers
entry) whose secrets are referenced as secret://NAME, so the existing
missing-secret flow prompts for them. Catalog data lives in catalog.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Field:
    """A non-secret parameter the user supplies (channel, repo, from, ...)."""
    name: str
    placeholder: str
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class SecretNeed:
    name: str            # the secret:// name the rendered node references
    how: str             # one-line hint on where to get it


@dataclass
class ConnectorResult:
    node: dict[str, Any]
    secret_names: list[str]
    mcp_servers: dict[str, Any] | None = None
    note: str | None = None


Renderer = Callable[[str, dict[str, Any]], ConnectorResult]


@dataclass(frozen=True)
class Connector:
    key: str
    category: str
    title: str
    description: str
    kind: Literal["http", "function", "mcp"]
    render_fn: Renderer
    secrets: tuple[SecretNeed, ...] = ()
    fields: tuple[Field, ...] = ()
    extra: str | None = None          # pip extra needed to RUN it (postgres/s3)

    def missing_fields(self, values: dict[str, Any]) -> list[str]:
        return [f.name for f in self.fields
                if f.required and f.default is None and not values.get(f.name)]

    def render(self, node_id: str, values: dict[str, Any]) -> ConnectorResult:
        merged = {f.name: f.default for f in self.fields if f.default is not None}
        merged.update({k: v for k, v in values.items() if v is not None})
        return self.render_fn(node_id, merged)


_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    if connector.key in _REGISTRY:
        raise ValueError(f"connector '{connector.key}' already registered")
    _REGISTRY[connector.key] = connector
    return connector


def get(key: str) -> Connector:
    _load()
    if key not in _REGISTRY:
        raise KeyError(f"unknown connector '{key}' (have: {', '.join(sorted(_REGISTRY))})")
    return _REGISTRY[key]


def all_connectors() -> list[Connector]:
    _load()
    return sorted(_REGISTRY.values(), key=lambda c: (c.category, c.key))


def categories() -> list[str]:
    return sorted({c.category for c in all_connectors()})


_loaded = False


def _load() -> None:
    global _loaded
    if not _loaded:
        _loaded = True
        from . import catalog  # noqa: F401 — registers connectors on import
