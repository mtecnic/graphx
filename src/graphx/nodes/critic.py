"""critic node: independent review of an artifact (self-review safeguard).

The critic judges ONE artifact against criteria in a FRESH context — it
never inherits the producing agent's conversation (the handler only
receives the resolved `artifact` value), which structurally prevents the
same-model-writes-and-grades blind spot. Use a different model than the
producer for a truly independent review, or a deterministic `handler`
for an objective gate. The verdict routes via edges (loop on evidence):

    - { from: review, to: publish, when: "review.verdict == 'pass'" }
    - { from: review, to: writer,  when: "review.verdict == 'fail'" }
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, model_validator

from ..engine.errors import ConfigError, ValidationFailed
from ..llm.tools import tool_result_to_text
from .agent import extract_json
from .function import load_callable
from .registry import NodeContext, NodeMetrics, NodeResult, node_type

_SYSTEM = ("You are a strict, independent reviewer. Judge ONLY the artifact against "
           "the criteria — do not assume intent beyond what is shown. Reply with ONLY a "
           'JSON object: {"verdict": "pass" | "fail", "score": 0.0-1.0, "reasons": "..."}')


class CriticConfig(BaseModel):
    artifact: Any = None                 # <node.field>/<state.key> ref or inline text
    criteria: str
    model: str | None = None             # LLM critic (its OWN model)
    handler: str | None = None           # deterministic critic: "module:function"
    system: str | None = None
    min_score: float | None = None       # pass rule: score >= min_score
    require: Literal["pass"] | None = None   # pass rule: verdict == "pass"
    temperature: float | None = None
    on_pass: str | None = None
    on_fail: str | None = None
    feedback_channel: str | None = None  # write reasons here on fail (state)

    @model_validator(mode="after")
    def _one_backend(self) -> "CriticConfig":
        if bool(self.model) == bool(self.handler):
            raise ValueError("critic needs exactly one of 'model' or 'handler'")
        if self.min_score is None and self.require is None:
            self.require = "pass"        # default pass rule
        return self


def _passed(cfg: CriticConfig, verdict: str, score: float | None) -> bool:
    if cfg.min_score is not None:
        return score is not None and score >= cfg.min_score
    return verdict == "pass"


def _result(cfg: CriticConfig, verdict: str, score: float | None, reasons: str,
            metrics: NodeMetrics) -> NodeResult:
    passed = _passed(cfg, verdict, score)
    output = {"verdict": "pass" if passed else "fail", "score": score,
              "reasons": reasons, "raw_verdict": verdict}
    updates: dict[str, Any] = {}
    if cfg.feedback_channel and not passed:
        updates[cfg.feedback_channel] = reasons
    goto = None
    if cfg.on_pass and cfg.on_fail:
        goto = (cfg.on_pass,) if passed else (cfg.on_fail,)
    return NodeResult(output=output, updates=updates, goto=goto, metrics=metrics)


@node_type("critic", config_model=CriticConfig)
async def critic_node(ctx: NodeContext) -> NodeResult:
    cfg: CriticConfig = ctx.config  # type: ignore[assignment]

    # deterministic critic — a plain Python check, no LLM
    if cfg.handler:
        fn = load_callable(cfg.handler)
        import asyncio
        verdict_obj = (await fn(artifact=cfg.artifact, criteria=cfg.criteria)
                       if asyncio.iscoroutinefunction(fn)
                       else await asyncio.to_thread(fn, artifact=cfg.artifact,
                                                    criteria=cfg.criteria))
        if not isinstance(verdict_obj, dict) or "verdict" not in verdict_obj:
            raise ConfigError(f"critic handler '{cfg.handler}' must return "
                              '{"verdict","score","reasons"}')
        return _result(cfg, str(verdict_obj.get("verdict")),
                       verdict_obj.get("score"), str(verdict_obj.get("reasons", "")),
                       NodeMetrics())

    # LLM critic — FRESH messages: criteria + artifact only (separate context)
    llm = ctx.services.llm
    if llm is None:
        raise ConfigError("no LLM client configured (services.llm is None)")
    model = ctx.model_override or cfg.model
    base = [
        {"role": "system", "content": cfg.system or _SYSTEM},
        {"role": "user", "content": f"CRITERIA:\n{cfg.criteria}\n\nARTIFACT:\n"
         f"{tool_result_to_text(cfg.artifact)}"},
    ]
    messages = list(base)
    metrics = NodeMetrics()
    reasks = ctx.node.resilience.validation_reasks
    last_error = ""
    for attempt in range(reasks + 1):
        response = await llm.chat(model, messages, temperature=cfg.temperature,
                                  force_json=True)
        metrics.tokens += response.usage.total
        metrics.cost_usd += response.usage.cost_usd
        try:
            parsed = extract_json(response.text)
            verdict = str(parsed.get("verdict", "")).lower()
            if verdict not in ("pass", "fail"):
                raise ValueError(f"verdict must be 'pass' or 'fail', got {verdict!r}")
            score = parsed.get("score")
            score = float(score) if score is not None else None
            return _result(cfg, verdict, score, str(parsed.get("reasons", "")), metrics)
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            messages.append({"role": "assistant", "content": response.text})
            messages.append({"role": "user", "content":
                             f"Invalid: {last_error[:300]}. Reply with ONLY "
                             '{"verdict":"pass|fail","score":0-1,"reasons":"..."}'})

    raise ValidationFailed(f"critic '{ctx.node.id}' produced no valid verdict after "
                           f"{reasks} re-ask(s): {last_error[:300]}")
