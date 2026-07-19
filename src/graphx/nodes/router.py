"""router node: an LLM chooses the next node from a fixed set of options."""

from __future__ import annotations

import json

from pydantic import BaseModel

from ..engine.errors import ConfigError, ValidationFailed
from .agent import extract_json
from .registry import NodeContext, NodeMetrics, NodeResult, node_type


class RouterConfig(BaseModel):
    model: str
    prompt: str
    options: dict[str, str]     # node id -> description of when to pick it
    system: str | None = None
    temperature: float | None = None


@node_type("router", config_model=RouterConfig)
async def router_node(ctx: NodeContext) -> NodeResult:
    config: RouterConfig = ctx.config  # type: ignore[assignment]
    llm = ctx.services.llm
    if llm is None:
        raise ConfigError("no LLM client configured (services.llm is None)")
    model = ctx.model_override or config.model
    if not config.options:
        raise ConfigError("router needs at least one option")

    option_lines = "\n".join(f"- {nid}: {why}" for nid, why in config.options.items())
    messages = []
    if config.system:
        messages.append({"role": "system", "content": config.system})
    messages.append({"role": "user", "content": (
        f"{config.prompt}\n\nChoose exactly one route:\n{option_lines}\n\n"
        'Respond with ONLY a JSON object: {"choice": "<route id>", "reason": "<short why>"}')})

    metrics = NodeMetrics()
    reasks = ctx.node.resilience.validation_reasks
    last_error = ""
    for attempt in range(reasks + 1):
        response = await llm.chat(model, messages, temperature=config.temperature,
                                  force_json=True)
        metrics.tokens += response.usage.total
        metrics.cost_usd += response.usage.cost_usd
        try:
            parsed = extract_json(response.text)
            choice = parsed.get("choice")
            if choice not in config.options:
                raise ValueError(f"choice {choice!r} is not one of "
                                 f"{list(config.options)}")
            return NodeResult(output={"choice": choice,
                                      "reason": parsed.get("reason", "")},
                              goto=(choice,), metrics=metrics)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content":
                             f"Invalid: {last_error[:300]}. Respond with ONLY the JSON "
                             f'object {{"choice": ...}} using one of: '
                             f"{', '.join(config.options)}"})

    raise ValidationFailed(f"router '{ctx.node.id}' failed to produce a valid choice "
                           f"after {reasks} re-ask(s): {last_error[:300]}")
