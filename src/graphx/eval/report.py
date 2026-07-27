"""Eval + compare report dataclasses and renderers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .assertions import CheckResult
from .metrics import RunMetricsReport


@dataclass
class CaseResult:
    name: str
    passed: bool
    checks: list[CheckResult]
    metrics: RunMetricsReport
    final_state: dict
    run_id: str
    error: str | None = None


@dataclass
class EvalReport:
    dataset: str
    workflow: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if not c.passed)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.metrics.cost_usd for c in self.cases), 6)

    @property
    def total_tokens(self) -> int:
        return sum(c.metrics.tokens for c in self.cases)

    def render(self, console) -> None:
        for case in self.cases:
            mark = "[green]PASS[/green]" if case.passed else "[red]FAIL[/red]"
            console.print(f"{mark} [bold]{case.name}[/bold]  "
                          f"[dim]{case.metrics.tokens} tok, ${case.metrics.cost_usd:g}, "
                          f"{case.metrics.duration_s:g}s[/dim]")
            if case.error:
                console.print(f"    [red]error: {case.error}[/red]")
            for chk in case.checks:
                icon = "[green]✔[/green]" if chk.ok else "[red]✘[/red]"
                console.print(f"    {icon} {chk.detail}")
        console.print(f"\n[bold]{self.passed}/{len(self.cases)} passed[/bold]  "
                      f"[dim]{self.total_tokens} tok, ${self.total_cost_usd:g}[/dim]")


@dataclass
class CaseDiff:
    name: str
    a: CaseResult
    b: CaseResult
    trace_diff: list[str]

    @property
    def outcome_changed(self) -> bool:
        return self.a.passed != self.b.passed or \
            self.a.metrics.status != self.b.metrics.status


@dataclass
class CompareReport:
    dataset: str
    label_a: str
    label_b: str
    diffs: list[CaseDiff] = field(default_factory=list)

    def render(self, console) -> None:
        for d in self.diffs:
            flip = " [yellow](outcome changed)[/yellow]" if d.outcome_changed else ""
            console.print(f"[bold]{d.name}[/bold]{flip}")
            console.print(f"  {self.label_a}: {'PASS' if d.a.passed else 'FAIL'}  "
                          f"{d.a.metrics.tokens} tok, ${d.a.metrics.cost_usd:g}")
            console.print(f"  {self.label_b}: {'PASS' if d.b.passed else 'FAIL'}  "
                          f"{d.b.metrics.tokens} tok, ${d.b.metrics.cost_usd:g}  "
                          f"[dim](Δ {d.b.metrics.tokens - d.a.metrics.tokens:+d} tok)[/dim]")
            if d.trace_diff:
                console.print("  [dim]trace diff:[/dim]")
                for line in d.trace_diff[:12]:
                    style = "green" if line.startswith("+") else \
                        "red" if line.startswith("-") else "dim"
                    console.print(f"    [{style}]{line}[/{style}]", highlight=False)
