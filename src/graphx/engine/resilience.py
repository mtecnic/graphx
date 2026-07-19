"""Retry / timeout / fallback wrapper around a node handler.

Three retry classes, kept strictly apart:
  - transient retry: backoff+jitter inside one variant (model)
  - model fallback:  advance to the next variant after a variant fails
  - validation reask: lives inside the agent node handler (v0.2), capped
                      by policy.validation_reasks

InterruptSignal and CancelledError always propagate immediately — they
are control flow, not failures.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..model.graph import ResiliencePolicy, StaticFallback
from ..nodes.registry import NodeContext, NodeMetrics, NodeResult
from .errors import InterruptSignal, NodeTimeout, is_transient
from .events import EventBus, EventType
from .services import Services


@dataclass
class Settled:
    ok: bool
    result: NodeResult | None = None
    error: BaseException | None = None
    attempts: int = 0
    duration_s: float = 0.0


def backoff_delay(attempt: int, policy_base: float, factor: float, max_delay: float,
                  jitter: bool, rng, retry_after: float | None) -> float:
    delay = min(policy_base * (factor ** (attempt - 1)), max_delay)
    if jitter:
        delay = delay / 2 + rng.random() * delay / 2
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


async def run_with_resilience(
    handler, ctx: NodeContext, policy: ResiliencePolicy,
    bus: EventBus, services: Services,
) -> Settled:
    retry = policy.retry
    started = services.clock.monotonic()
    total_attempts = 0
    last_error: BaseException | None = None
    variants: list[str | StaticFallback | None] = [None, *policy.fallbacks]

    for variant in variants:
        if isinstance(variant, StaticFallback):
            await bus.emit(EventType.NODE_FALLBACK, step=ctx.step, node_id=ctx.node.id,
                           to="static", degraded=True)
            result = NodeResult(output=dict(variant.output),
                                metrics=NodeMetrics(attempts=total_attempts + 1, degraded=True))
            return Settled(ok=True, result=result, attempts=total_attempts + 1,
                           duration_s=services.clock.monotonic() - started)
        if isinstance(variant, str):
            ctx.model_override = variant
            await bus.emit(EventType.NODE_FALLBACK, step=ctx.step, node_id=ctx.node.id,
                           to=variant)

        for attempt in range(1, retry.attempts + 1):
            total_attempts += 1
            try:
                if policy.timeout_s is not None:
                    try:
                        async with asyncio.timeout(policy.timeout_s):
                            result = await handler(ctx)
                    except TimeoutError as exc:
                        raise NodeTimeout(
                            f"node '{ctx.node.id}' timed out after {policy.timeout_s:g}s"
                        ) from exc
                else:
                    result = await handler(ctx)
            except (InterruptSignal, asyncio.CancelledError):
                raise
            except BaseException as exc:  # noqa: BLE001 — settled, not swallowed
                last_error = exc
                if is_transient(exc) and attempt < retry.attempts:
                    delay = backoff_delay(
                        attempt, retry.base, retry.factor, retry.max_delay,
                        retry.jitter, services.rng,
                        getattr(exc, "retry_after", None),
                    )
                    await bus.emit(EventType.NODE_RETRYING, step=ctx.step,
                                   node_id=ctx.node.id, attempt=attempt,
                                   delay_s=round(delay, 3), error=str(exc),
                                   error_type=type(exc).__name__)
                    await services.clock.sleep(delay)
                    continue
                break  # deterministic error or retries exhausted -> next variant
            else:
                result.metrics.attempts = total_attempts
                result.metrics.duration_s = services.clock.monotonic() - started
                return Settled(ok=True, result=result, attempts=total_attempts,
                               duration_s=result.metrics.duration_s)

    return Settled(ok=False, error=last_error, attempts=total_attempts,
                   duration_s=services.clock.monotonic() - started)
