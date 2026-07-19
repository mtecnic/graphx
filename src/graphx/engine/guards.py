"""Four independent guards; the first to trip halts the run.

1. max_steps       — superstep ceiling (bounds all loops)
2. token budget    — cumulative tokens across all nodes
3. cost budget     — cumulative estimated cost
4. deadline        — wall-clock for the whole run

(Retry exhaustion is the fifth failure mode but is handled per-node by
the resilience layer, not here.)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model.graph import GraphConfig
from .checkpoint import RunMetrics
from .errors import GuardTripped
from .services import Clock


@dataclass
class GuardStatus:
    guard: str
    used: float
    limit: float

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 0.0


class Guards:
    def __init__(self, config: GraphConfig, clock: Clock,
                 already_elapsed_s: float = 0.0):
        self.config = config
        self.clock = clock
        self.started = clock.monotonic() - already_elapsed_s

    def elapsed_s(self) -> float:
        return self.clock.monotonic() - self.started

    def statuses(self, step: int, metrics: RunMetrics) -> list[GuardStatus]:
        out = [GuardStatus("max_steps", step, self.config.max_steps)]
        budget = self.config.budget
        if budget:
            if budget.tokens is not None:
                out.append(GuardStatus("tokens", metrics.tokens, budget.tokens))
            if budget.cost_usd is not None:
                out.append(GuardStatus("cost_usd", metrics.cost_usd, budget.cost_usd))
            if budget.deadline_s is not None:
                out.append(GuardStatus("deadline", self.elapsed_s(), budget.deadline_s))
        return out

    def check(self, step: int, metrics: RunMetrics) -> None:
        """Raise GuardTripped if any limit is reached."""
        for status in self.statuses(step, metrics):
            if status.used >= status.limit:
                raise GuardTripped(
                    status.guard,
                    f"{status.used:g} >= limit {status.limit:g}",
                )

    def warnings(self, step: int, metrics: RunMetrics,
                 threshold: float = 0.8) -> list[GuardStatus]:
        return [s for s in self.statuses(step, metrics) if s.fraction >= threshold]
