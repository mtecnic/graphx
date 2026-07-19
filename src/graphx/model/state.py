"""Typed shared state: channels with reducers.

State is immutable; every superstep produces a new State via apply().
Reducers make concurrent writes and loop re-entry safe: a channel with
reducer "append" accumulates, "last" overwrites, etc.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Reducer = Callable[[Any, Any], Any]


def _reduce_last(current: Any, update: Any) -> Any:
    return update


def _reduce_append(current: Any, update: Any) -> Any:
    out = list(current or [])
    out.append(update)
    return out


def _reduce_extend(current: Any, update: Any) -> Any:
    out = list(current or [])
    if isinstance(update, (list, tuple)):
        out.extend(update)
    else:
        out.append(update)
    return out


def _reduce_sum(current: Any, update: Any) -> Any:
    return (current or 0) + update


def _reduce_merge_dict(current: Any, update: Any) -> Any:
    out = dict(current or {})
    out.update(update or {})
    return out


REDUCERS: dict[str, Reducer] = {
    "last": _reduce_last,
    "append": _reduce_append,
    "extend": _reduce_extend,
    "sum": _reduce_sum,
    "merge_dict": _reduce_merge_dict,
}

_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


class ChannelTypeError(TypeError):
    pass


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    type_: str = "any"
    reducer: str = "last"
    default: Any = None

    def check(self, value: Any) -> None:
        if value is None or self.type_ == "any":
            return
        expected = _TYPE_CHECKS.get(self.type_)
        if expected and not isinstance(value, expected):
            raise ChannelTypeError(
                f"channel '{self.name}' expects {self.type_}, got {type(value).__name__}"
            )


@dataclass(frozen=True)
class State:
    channels: Mapping[str, ChannelSpec]
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def initial(cls, channels: Mapping[str, ChannelSpec],
                inputs: Mapping[str, Any] | None = None) -> State:
        values = {name: spec.default for name, spec in channels.items()}
        state = cls(channels=dict(channels), values=values)
        return state.apply(inputs or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def apply(self, updates: Mapping[str, Any]) -> State:
        if not updates:
            return self
        new_values = dict(self.values)
        for key, update in updates.items():
            spec = self.channels.get(key)
            if spec is None:
                raise KeyError(f"unknown state channel '{key}'")
            reducer = REDUCERS[spec.reducer]
            merged = reducer(new_values.get(key), update)
            spec.check(merged)
            new_values[key] = merged
        return State(channels=self.channels, values=new_values)

    def to_json(self) -> dict[str, Any]:
        return dict(self.values)

    @classmethod
    def from_json(cls, channels: Mapping[str, ChannelSpec], data: Mapping[str, Any]) -> State:
        values = {name: spec.default for name, spec in channels.items()}
        values.update(data)
        return cls(channels=dict(channels), values=values)
