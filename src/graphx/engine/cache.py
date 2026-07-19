"""Canonical input hashing for node-result memoization / idempotency."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def input_hash(node_type: str, resolved_config: Any, item: Any = None) -> str:
    payload = json.dumps(
        {"type": node_type, "config": _canonical(resolved_config), "item": _canonical(item)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()
