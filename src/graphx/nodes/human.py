"""human node: approval/input gate.

First execution interrupts the run (state checkpointed, thread parked).
Resuming with Command(resume=value) re-executes this node with
ctx.resume_value set; the answer becomes the node output, addressable
as <gate.choice> / <gate.answer>.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..engine.errors import ConfigError
from .registry import NodeContext, NodeResult, node_type


class HumanConfig(BaseModel):
    prompt: str
    choices: list[str] | None = None
    payload: Any = None


@node_type("human", config_model=HumanConfig)
async def human_node(ctx: NodeContext) -> NodeResult:
    config: HumanConfig = ctx.config  # type: ignore[assignment]
    if ctx.resume_value is None:
        ctx.interrupt({
            "prompt": config.prompt,
            "choices": config.choices,
            "payload": config.payload,
        })
    answer = ctx.resume_value
    if config.choices and answer not in config.choices:
        raise ConfigError(
            f"answer {answer!r} is not one of the allowed choices {config.choices}")
    return NodeResult(output={"choice": answer, "answer": answer})
