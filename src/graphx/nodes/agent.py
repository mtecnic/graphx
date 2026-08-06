"""agent node: an LLM call with tools, streaming, and validated output.

The three retry classes stay separate here too: this handler implements
only the *validation re-ask* loop (feed the schema error back, capped
by policy.validation_reasks). Transient retries and model fallbacks
wrap it from the outside via the resilience layer — a ValidationFailed
after all re-asks is deterministic, so the fallback chain advances to
the next model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

from ..engine.errors import ConfigError, GraphxError, ValidationFailed
from ..llm.client import ToolDef
from ..llm.tools import resolve_tools, tool_result_to_text
from .registry import NodeContext, NodeMetrics, NodeResult, node_type

_TYPE_MAP: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool,
                              "list": list, "dict": dict, "any": object}


class AgentConfig(BaseModel):
    model: str
    prompt: str
    system: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    output_schema: dict[str, str] | None = None
    max_tool_rounds: int = 5
    temperature: float | None = None
    max_tokens: int | None = None
    handoffs: list[str] = Field(default_factory=list)   # target node ids to hand off to
    handoff_channel: str = "handoff"                    # state channel for transferred context


HANDOFF_PREFIX = "handoff_to_"


def build_schema_model(schema: dict[str, str]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for name, type_name in schema.items():
        py_type = _TYPE_MAP.get(type_name)
        if py_type is None:
            raise ConfigError(f"output_schema: unknown type '{type_name}' "
                              f"(have: {', '.join(_TYPE_MAP)})")
        fields[name] = (py_type, ...)
    return create_model("AgentOutput", **fields)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a possibly chatty/fenced response."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start:end + 1])


@node_type("agent", config_model=AgentConfig)
async def agent_node(ctx: NodeContext) -> NodeResult:
    config: AgentConfig = ctx.config  # type: ignore[assignment]
    llm = ctx.services.llm
    if llm is None:
        raise ConfigError("no LLM client configured (services.llm is None)")
    model = ctx.model_override or config.model

    messages: list[dict[str, Any]] = []
    if config.system:
        messages.append({"role": "system", "content": config.system})
    prompt = config.prompt
    schema_model: type[BaseModel] | None = None
    if config.output_schema:
        schema_model = build_schema_model(config.output_schema)
        prompt += ("\n\nRespond with a single JSON object with exactly these fields: "
                   + json.dumps(config.output_schema) + ". No other text.")
    messages.append({"role": "user", "content": prompt})

    tools = await resolve_tools(config.tools, ctx.services)
    tool_defs = [t.definition for t in tools]
    by_name = {t.definition.name: t for t in tools}

    # synthetic handoff tools: calling one transfers control + this conversation
    # to a specialist node (flow=goto, data=state channel, logic=here).
    for target in config.handoffs:
        tool_defs.append(ToolDef(
            name=f"{HANDOFF_PREFIX}{target}",
            description=f"Transfer this task and conversation to '{target}' to continue.",
            parameters={"type": "object",
                        "properties": {"reason": {"type": "string",
                                                  "description": "why you are handing off"}},
                        "required": ["reason"]}))

    metrics = NodeMetrics()
    budget = ctx.node.resilience.budget

    async def check_budget() -> None:
        if budget and budget.tokens is not None and metrics.tokens > budget.tokens:
            raise GraphxError(
                f"agent '{ctx.node.id}' exceeded its token budget "
                f"({metrics.tokens} > {budget.tokens})")

    async def call_llm(msgs: list[dict[str, Any]], with_tools: bool):
        response = await llm.chat(
            model, msgs, tools=tool_defs if with_tools and tool_defs else None,
            stream_cb=ctx.emit_chunk, temperature=config.temperature,
            max_tokens=config.max_tokens, force_json=schema_model is not None)
        metrics.tokens += response.usage.total
        metrics.cost_usd += response.usage.cost_usd
        await check_budget()
        return response

    # ---- tool loop ----
    response = await call_llm(messages, with_tools=True)
    rounds = 0
    while response.tool_calls:
        rounds += 1
        if rounds > config.max_tool_rounds:
            raise GraphxError(f"agent '{ctx.node.id}' exceeded max_tool_rounds "
                              f"({config.max_tool_rounds})")
        # a handoff short-circuits the loop: transfer control + this conversation
        for call in response.tool_calls:
            if call.name.startswith(HANDOFF_PREFIX):
                target = call.name[len(HANDOFF_PREFIX):]
                reason = call.arguments.get("reason", "") if isinstance(
                    call.arguments, dict) else ""
                context = {"messages": messages, "reason": reason,
                           "from": ctx.node.id, "artifact": response.text or ""}
                return NodeResult(
                    output={"handoff_to": target, "reason": reason},
                    updates={config.handoff_channel: context},
                    goto=(target,), metrics=metrics)
        messages.append({"role": "assistant", "content": response.text or None,
                         "tool_calls": [{"id": c.id, "type": "function", "function": {
                             "name": c.name, "arguments": json.dumps(c.arguments)}}
                             for c in response.tool_calls]})
        for call in response.tool_calls:
            tool = by_name.get(call.name)
            if tool is None:
                result_text = f"error: unknown tool '{call.name}'"
            else:
                try:
                    result_text = tool_result_to_text(await tool.call(call.arguments))
                except Exception as exc:  # noqa: BLE001 — fed back to the model
                    result_text = f"error: {type(exc).__name__}: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": result_text})
        response = await call_llm(messages, with_tools=True)

    # ---- validation re-ask loop (its own retry class) ----
    if schema_model is None:
        return NodeResult(output={"text": response.text}, metrics=metrics)

    reasks = ctx.node.resilience.validation_reasks
    last_error = ""
    for attempt in range(reasks + 1):
        try:
            parsed = extract_json(response.text)
            validated = schema_model.model_validate(parsed)
            output = validated.model_dump()
            # raw response under "text" only when the schema doesn't claim
            # that key — a validated field must never be clobbered by the
            # raw JSON blob it was parsed from
            output.setdefault("text", response.text)
            return NodeResult(output=output, metrics=metrics)
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            if attempt == reasks:
                break
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content":
                             f"Your response failed validation: {last_error[:500]}\n"
                             "Reply again with ONLY the corrected JSON object."})
            response = await call_llm(messages, with_tools=False)

    raise ValidationFailed(
        f"agent '{ctx.node.id}' output failed schema validation after "
        f"{reasks} re-ask(s): {last_error[:300]}")
