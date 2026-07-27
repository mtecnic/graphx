"""Derive per-run and per-node metrics reports from stored event data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..engine.checkpoint import RunMetrics
from ..engine.events import EventType, RunEvent

_TERMINAL = {EventType.RUN_FINISHED: "finished", EventType.RUN_FAILED: "failed",
             EventType.RUN_INTERRUPTED: "interrupted", EventType.RUN_CANCELLED: "cancelled"}


@dataclass
class NodeMetricsReport:
    node_id: str
    runs: int = 0
    latency_s: float = 0.0
    attempts: int = 0
    retries: int = 0
    fallbacks: int = 0
    failures: int = 0
    cached: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class RunMetricsReport:
    run_id: str
    thread_id: str
    workflow: str
    status: str
    steps: int = 0
    duration_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    nodes_run: int = 0
    retries: int = 0
    fallbacks: int = 0
    dead_letters: int = 0
    interventions: int = 0
    per_node: list[NodeMetricsReport] = field(default_factory=list)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def derive_run_report(events: list[RunEvent],
                      run_metrics: RunMetrics | None = None) -> RunMetricsReport:
    nodes: dict[str, NodeMetricsReport] = {}

    def node(nid: str) -> NodeMetricsReport:
        return nodes.setdefault(nid, NodeMetricsReport(node_id=nid))

    status = "running"
    steps = 0
    dead_letters = interventions = 0
    times = [t for e in events if (t := _parse_ts(e.ts))]
    run_id = events[0].run_id if events else ""
    thread_id = events[0].thread_id if events else ""
    workflow = ""

    for e in events:
        steps = max(steps, e.step)
        if e.type == EventType.RUN_STARTED:
            workflow = e.data.get("graph", "")
        elif e.type in _TERMINAL:
            status = _TERMINAL[e.type]
            dead_letters = max(dead_letters, int(e.data.get("dead_letters", 0) or 0))
        elif e.type == EventType.INTERRUPT_RAISED:
            interventions += 1
        elif e.node_id:
            n = node(e.node_id)
            if e.type == EventType.NODE_FINISHED:
                n.runs += 1
                n.latency_s += float(e.data.get("duration_s", 0) or 0)
                n.attempts += int(e.data.get("attempts", 1) or 1)
                n.tokens += int(e.data.get("tokens", 0) or 0)
                n.cost_usd += float(e.data.get("cost_usd", 0) or 0)
                if e.data.get("cached"):
                    n.cached += 1
            elif e.type == EventType.NODE_RETRYING:
                n.retries += 1
            elif e.type == EventType.NODE_FALLBACK:
                n.fallbacks += 1
            elif e.type == EventType.NODE_FAILED:
                n.failures += 1

    duration = (max(times) - min(times)).total_seconds() if len(times) >= 2 else 0.0
    tokens = run_metrics.tokens if run_metrics else sum(n.tokens for n in nodes.values())
    cost = run_metrics.cost_usd if run_metrics else sum(n.cost_usd for n in nodes.values())
    nodes_run = run_metrics.nodes_run if run_metrics else sum(n.runs for n in nodes.values())

    return RunMetricsReport(
        run_id=run_id, thread_id=thread_id, workflow=workflow, status=status,
        steps=steps, duration_s=round(duration, 3), tokens=tokens,
        cost_usd=round(cost, 6), nodes_run=nodes_run,
        retries=sum(n.retries for n in nodes.values()),
        fallbacks=sum(n.fallbacks for n in nodes.values()),
        dead_letters=dead_letters, interventions=interventions,
        per_node=sorted(nodes.values(), key=lambda n: n.node_id))


@dataclass
class AggregateReport:
    runs: int = 0
    passed: int = 0
    failed: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_duration_s: float = 0.0


def aggregate(reports: list[RunMetricsReport]) -> AggregateReport:
    if not reports:
        return AggregateReport()
    return AggregateReport(
        runs=len(reports),
        passed=sum(1 for r in reports if r.status == "finished"),
        failed=sum(1 for r in reports if r.status in ("failed", "guard_tripped")),
        total_tokens=sum(r.tokens for r in reports),
        total_cost_usd=round(sum(r.cost_usd for r in reports), 6),
        avg_duration_s=round(sum(r.duration_s for r in reports) / len(reports), 3))
