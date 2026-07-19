"""Reference tags: the data-flow half of the graph.

Grammar (inside node config values):
    <state.KEY[.path...]>   read a state channel (dotted path into it)
    <NODE_ID.FIELD[.path]>  read a field of that node's last output
    <item.X>                current item inside a map template
    <self.X>                a node's own output, in its updates: block
    ${ENV_VAR}              environment variable, resolved at load time

A string that is exactly one ref resolves to the referenced object with
its type preserved; refs embedded in a longer string are interpolated
as str().
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_REF_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)>")


class RefError(KeyError):
    pass


@dataclass(frozen=True)
class RefContext:
    state: Mapping[str, Any] = field(default_factory=dict)
    node_outputs: Mapping[str, Any] = field(default_factory=dict)
    item: Any = None
    self_output: Any = None


def find_refs(value: Any) -> set[str]:
    """All ref expressions (dotted paths, no angle brackets) in a config tree."""
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(m.group(1) for m in _REF_RE.finditer(value))
    elif isinstance(value, Mapping):
        for v in value.values():
            refs.update(find_refs(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            refs.update(find_refs(v))
    return refs


def _dig(obj: Any, path: list[str], full: str) -> Any:
    for part in path:
        if isinstance(obj, Mapping):
            if part not in obj:
                raise RefError(f"<{full}>: key '{part}' not found")
            obj = obj[part]
        elif isinstance(obj, (list, tuple)):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError) as exc:
                raise RefError(f"<{full}>: bad index '{part}'") from exc
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            raise RefError(f"<{full}>: cannot descend into {type(obj).__name__} with '{part}'")
    return obj


def resolve_ref(expr: str, ctx: RefContext) -> Any:
    head, *rest = expr.split(".")
    if head == "state":
        if not rest:
            raise RefError(f"<{expr}>: state ref needs a key")
        if rest[0] not in ctx.state:
            raise RefError(f"<{expr}>: unknown state channel '{rest[0]}'")
        return _dig(ctx.state[rest[0]], rest[1:], expr)
    if head == "item":
        return _dig(ctx.item, rest, expr)
    if head == "self":
        return _dig(ctx.self_output, rest, expr)
    if head not in ctx.node_outputs:
        raise RefError(f"<{expr}>: no output recorded for node '{head}'")
    return _dig(ctx.node_outputs[head], rest, expr)


def resolve(value: Any, ctx: RefContext) -> Any:
    """Recursively resolve refs in a config tree, preserving types for whole-string refs."""
    if isinstance(value, str):
        match = _REF_RE.fullmatch(value.strip())
        if match:
            return resolve_ref(match.group(1), ctx)
        return _REF_RE.sub(lambda m: str(resolve_ref(m.group(1), ctx)), value)
    if isinstance(value, Mapping):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve(v, ctx) for v in value)
    return value
