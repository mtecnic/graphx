"""Frontier computation: routing, barrier joins, dynamic Sends, loop guards.

Deterministic by construction: settled results are processed in sorted
node-id order and edges in declaration order, so the same run always
produces the same schedule (locked in by the golden trace tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.exprs import build_namespace, evaluate
from ..model.graph import END, Graph
from .checkpoint import TaskSpec


@dataclass
class RouteOutcome:
    frontier: list[TaskSpec] = field(default_factory=list)
    fatal_failures: list[str] = field(default_factory=list)   # node ids that sank the run
    exhausted: list[str] = field(default_factory=list)        # nodes routed via on_exhausted


def _is_merge(graph: Graph, node_id: str) -> bool:
    node = graph.nodes.get(node_id)
    return node is not None and node.type == "merge"


class Scheduler:
    def __init__(self, graph: Graph):
        self.graph = graph

    def _schedule(self, target: str, outcome: RouteOutcome, visits: dict[str, int],
                  item: Any = None, collect_channel: str | None = None) -> None:
        if target == END:
            return
        node = self.graph.nodes[target]
        next_visit = visits.get(target, 0) + 1
        if node.max_iterations is not None and next_visit > node.max_iterations:
            outcome.exhausted.append(target)
            if node.on_exhausted:
                self._schedule(node.on_exhausted, outcome, visits)
            else:
                outcome.fatal_failures.append(target)
            return
        visits[target] = next_visit
        outcome.frontier.append(TaskSpec(node_id=target, item=item,
                                         collect_channel=collect_channel))

    def _arrive_at_merge(self, merge_id: str, source: str, state: str,
                         join_arrivals: dict[str, dict[str, str]],
                         outcome: RouteOutcome, visits: dict[str, int]) -> None:
        # state is one of "ok" | "failed" | "skipped" — a skipped branch must
        # neither count against the merge nor make it wait forever.
        arrivals = join_arrivals.setdefault(merge_id, {})
        arrivals[source] = state
        expected = {e.source for e in self.graph.edges_to(merge_id)}
        if set(arrivals) >= expected:
            self._schedule(merge_id, outcome, visits,
                           item={"arrivals": dict(arrivals)})
            join_arrivals.pop(merge_id, None)

    def flush_pending_merges(self, join_arrivals: dict[str, dict[str, str]],
                             visits: dict[str, int]) -> RouteOutcome:
        """Fire merges still waiting on branches that will never arrive.

        Called when the frontier is empty (no further supersteps): any source
        a pending merge is still expecting was pruned upstream, so treat the
        missing ones as skipped and let the merge proceed with what it has.
        """
        outcome = RouteOutcome()
        for merge_id, arrivals in list(join_arrivals.items()):
            full = dict(arrivals)
            for src in {e.source for e in self.graph.edges_to(merge_id)}:
                full.setdefault(src, "skipped")
            join_arrivals.pop(merge_id, None)
            self._schedule(merge_id, outcome, visits, item={"arrivals": full})
        return outcome

    def route_success(self, node_id: str, result_goto: tuple[str, ...] | None,
                      sends: tuple, state_values: dict[str, Any],
                      node_outputs: dict[str, Any], visits: dict[str, int],
                      join_arrivals: dict[str, dict[str, bool]],
                      outcome: RouteOutcome) -> None:
        namespace = build_namespace(state_values, node_outputs)
        edges = self.graph.edges_from(node_id)

        if result_goto is not None:
            targets = list(result_goto)
            # merge targets we routed away from arrive as "skipped" (not failed)
            for edge in edges:
                if edge.target not in targets and _is_merge(self.graph, edge.target):
                    self._arrive_at_merge(edge.target, node_id, "skipped",
                                          join_arrivals, outcome, visits)
        else:
            targets = []
            for edge in edges:
                taken = edge.when is None or bool(evaluate(edge.when, namespace))
                if taken:
                    targets.append(edge.target)
                elif _is_merge(self.graph, edge.target):
                    self._arrive_at_merge(edge.target, node_id, "skipped",
                                          join_arrivals, outcome, visits)

        for target in targets:
            if target != END and _is_merge(self.graph, target):
                self._arrive_at_merge(target, node_id, "ok",
                                      join_arrivals, outcome, visits)
            else:
                self._schedule(target, outcome, visits)

        for send in sends:
            self._schedule(send.target, outcome, visits,
                           item=send.payload, collect_channel=send.collect_channel)

    def route_failure(self, node_id: str, visits: dict[str, int],
                      join_arrivals: dict[str, dict[str, str]],
                      outcome: RouteOutcome) -> None:
        error_edges = self.graph.edges_from(node_id, kind="on_error")
        normal_targets = [e.target for e in self.graph.edges_from(node_id)]
        merge_targets = [t for t in normal_targets if _is_merge(self.graph, t)]

        if error_edges:
            for edge in error_edges:
                self._schedule(edge.target, outcome, visits)
            return
        if merge_targets and len(merge_targets) == len(normal_targets):
            # every downstream is a merge: settled semantics, branch failure tolerated
            for target in merge_targets:
                self._arrive_at_merge(target, node_id, "failed",
                                      join_arrivals, outcome, visits)
            return
        outcome.fatal_failures.append(node_id)
