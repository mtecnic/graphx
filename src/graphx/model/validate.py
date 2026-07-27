"""Static validation: catch broken graphs before running them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .graph import END, Graph
from .refs import find_refs


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.where}: {self.message}"


def _known_ref_heads(graph: Graph) -> set[str]:
    return {"state", "item", "self"} | set(graph.nodes)


def _find_cycles(graph: Graph) -> list[list[str]]:
    adjacency: dict[str, list[str]] = {n: [] for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in adjacency and edge.target in adjacency:
            adjacency[edge.source].append(edge.target)
    cycles: list[list[str]] = []
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in adjacency[node]:
            if color.get(nxt, 0) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif color.get(nxt, 0) == 0:
                dfs(nxt)
        stack.pop()
        color[node] = 2

    for node in graph.nodes:
        if color.get(node, 0) == 0:
            dfs(node)
    return cycles


def validate_graph(graph: Graph, known_types: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = []

    def err(where: str, msg: str) -> None:
        issues.append(Issue("error", where, msg))

    def warn(where: str, msg: str) -> None:
        issues.append(Issue("warning", where, msg))

    for entry in graph.entry:
        if entry not in graph.nodes:
            err("entry", f"unknown node '{entry}'")

    node_ids = set(graph.nodes)
    for edge in graph.edges:
        where = f"edge {edge.source}->{edge.target}"
        if edge.source not in node_ids:
            err(where, f"unknown source node '{edge.source}'")
        if edge.target not in node_ids and edge.target != END:
            err(where, f"unknown target node '{edge.target}'")

    ref_heads = _known_ref_heads(graph)
    for node in graph.nodes.values():
        where = f"node '{node.id}'"
        if known_types is not None and node.type not in known_types:
            err(where, f"unknown node type '{node.type}' (have: {', '.join(sorted(known_types))})")
        for ref in find_refs(dict(node.config)) | find_refs(dict(node.updates)):
            head, *rest = ref.split(".")
            if head not in ref_heads:
                err(where, f"ref <{ref}> points at unknown node/namespace '{head}'")
            elif head == "state" and rest and rest[0] not in graph.channels:
                err(where, f"ref <{ref}> points at unknown state channel '{rest[0]}'")
        for key in node.updates:
            if key not in graph.channels:
                err(where, f"updates writes unknown state channel '{key}'")
        if node.on_exhausted and node.on_exhausted not in node_ids and node.on_exhausted != END:
            err(where, f"on_exhausted -> unknown node '{node.on_exhausted}'")
        if node.type == "condition":
            for i, branch in enumerate(node.config.get("branches") or []):
                target = branch.get("goto") or branch.get("else")
                if target and target not in node_ids and target != END:
                    err(where, f"branches[{i}] -> unknown node '{target}'")
        # handoff targets + channel must exist
        for target in node.config.get("handoffs") or []:
            if target not in node_ids:
                err(where, f"handoff target '{target}' is not a node")
            elif graph.nodes[target].max_iterations is None:
                warn(where, f"handoff target '{target}' has no max_iterations; "
                            "an agent ping-pong is only bounded by config.max_steps")
        channel = node.config.get("handoff_channel")
        if node.config.get("handoffs") and channel and channel not in graph.channels:
            warn(where, f"handoff_channel '{channel}' is not a declared state channel; "
                        "the target won't be able to read <state.%s>" % channel)
        # critic reusing its producer's model gives a weaker, correlated review
        if node.type == "critic" and node.config.get("model"):
            for src_edge in graph.edges_to(node.id):
                producer = graph.nodes.get(src_edge.source)
                if producer and producer.config.get("model") == node.config["model"]:
                    warn(where, f"critic uses the same model as its producer "
                                f"'{producer.id}'; a different model gives a more "
                                "independent review")
                    break

    # A node with no outgoing edges (and not routing via condition) silently ends
    # its branch; that's legal but worth flagging when it looks accidental.
    for node in graph.nodes.values():
        if node.type in ("condition",):
            continue
        if not graph.edges_from(node.id):
            warn(f"node '{node.id}'", "no outgoing edge; this branch ends here")

    for cycle in _find_cycles(graph):
        guarded = any(
            graph.nodes[n].max_iterations is not None
            for n in cycle if n in graph.nodes
        )
        if not guarded:
            warn("cycle " + " -> ".join(cycle),
                 "loop has no node with max_iterations; only config.max_steps bounds it")

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
