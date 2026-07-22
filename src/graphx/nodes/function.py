"""function node: call a Python function `module.path:name` with kwargs.

The function may be sync or async. Its return value becomes the node
output (a dict return makes fields addressable as <node.field>).
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from pydantic import BaseModel, Field

from ..engine.errors import ConfigError
from .registry import NodeContext, NodeResult, node_type


class FunctionConfig(BaseModel):
    handler: str
    args: dict[str, Any] = Field(default_factory=dict)


def load_callable(spec: str):
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ConfigError(f"handler must look like 'package.module:function', got {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import module '{module_name}': {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise ConfigError(f"module '{module_name}' has no attribute '{attr}'") from exc


@node_type("function", config_model=FunctionConfig)
async def function_node(ctx: NodeContext) -> NodeResult:
    config: FunctionConfig = ctx.config  # type: ignore[assignment]
    fn = load_callable(config.handler)
    # resolve secret:// refs in args only now, at the call boundary
    args = ctx.services.secrets.resolve(dict(config.args))
    if asyncio.iscoroutinefunction(fn):
        output = await fn(**args)
    else:
        output = await asyncio.to_thread(fn, **args)
    return NodeResult(output=output)
