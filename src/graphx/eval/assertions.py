"""Deterministic checks over a finished run's final state + report."""

from __future__ import annotations

from dataclasses import dataclass

from ..model.exprs import ExprError, build_namespace, evaluate
from .dataset import Expect
from .metrics import RunMetricsReport


@dataclass(frozen=True)
class CheckResult:
    kind: str
    detail: str
    ok: bool


def check_expect(expect: Expect, status: str, dead_letters: int,
                 final_state: dict, node_outputs: dict,
                 report: RunMetricsReport) -> list[CheckResult]:
    checks: list[CheckResult] = []

    if expect.status is not None:
        checks.append(CheckResult("status", f"status == {expect.status}",
                                  status == expect.status))
    if expect.no_dead_letters:
        checks.append(CheckResult("dead_letters", "no dead-letters",
                                  dead_letters == 0))

    namespace = build_namespace(final_state, node_outputs)
    for expr in expect.assert_:
        try:
            ok = bool(evaluate(expr, namespace))
            checks.append(CheckResult("assert", expr, ok))
        except ExprError as exc:
            checks.append(CheckResult("assert", f"{expr}  ({exc})", False))

    budget = expect.budget or {}
    if "tokens" in budget:
        checks.append(CheckResult("budget", f"tokens {report.tokens} <= {budget['tokens']:g}",
                                  report.tokens <= budget["tokens"]))
    if "cost_usd" in budget:
        checks.append(CheckResult("budget",
                                  f"cost ${report.cost_usd:g} <= ${budget['cost_usd']:g}",
                                  report.cost_usd <= budget["cost_usd"]))
    return checks
