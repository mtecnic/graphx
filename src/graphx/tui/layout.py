"""Sugiyama layered layout (via grandalf) mapped onto a character grid."""

from __future__ import annotations

from dataclasses import dataclass

from grandalf.graphs import Edge as GEdge
from grandalf.graphs import Graph as GGraph
from grandalf.graphs import Vertex as GVertex
from grandalf.layouts import SugiyamaLayout

from ..model.graph import END, Graph

X_SPACE = 6   # horizontal gap between boxes
Y_SPACE = 3   # vertical gap between ranks
BOX_H = 3


@dataclass(frozen=True)
class NodeBox:
    node_id: str
    x: int
    y: int
    w: int
    h: int

    @property
    def top_anchor(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y)

    @property
    def bottom_anchor(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h - 1)


@dataclass(frozen=True)
class EdgeRoute:
    source: str
    target: str
    when: str | None
    kind: str
    back: bool          # loop-back (target above source)


@dataclass(frozen=True)
class GraphLayout:
    boxes: dict[str, NodeBox]
    edges: list[EdgeRoute]
    width: int
    height: int


class _View:
    def __init__(self, w: int, h: int):
        self.w = w
        self.h = h
        self.xy = (0.0, 0.0)


def _box_width(node_id: str, type_name: str) -> int:
    return max(len(node_id), len(type_name)) + 4


def _implicit_edges(graph: Graph) -> list[tuple[str, str, str]]:
    """Edges the engine routes dynamically: condition gotos, on_exhausted."""
    out: list[tuple[str, str, str]] = []
    for node in graph.nodes.values():
        if node.type == "condition":
            for branch in node.config.get("branches") or []:
                target = branch.get("goto") or branch.get("else")
                if target:
                    out.append((node.id, str(target), "goto"))
        if node.on_exhausted:
            out.append((node.id, node.on_exhausted, "goto"))
    explicit = {(e.source, e.target) for e in graph.edges}
    return [e for e in out if (e[0], e[1]) not in explicit]


def compute_layout(graph: Graph) -> GraphLayout:
    implicit = _implicit_edges(graph)
    show_end = any(e.target == END for e in graph.edges) or any(
        t == END for _, t, _ in implicit)
    ids = list(graph.nodes) + ([END] if show_end else [])

    vertices = {nid: GVertex(nid) for nid in ids}
    for nid, vertex in vertices.items():
        type_name = graph.nodes[nid].type if nid in graph.nodes else ""
        vertex.view = _View(_box_width(nid, type_name) + X_SPACE, BOX_H + Y_SPACE)

    all_pairs = [(e.source, e.target, e.kind, e.when) for e in graph.edges] + [
        (s, t, k, None) for s, t, k in implicit]
    gedges = [GEdge(vertices[s], vertices[t])
              for s, t, _, _ in all_pairs if s in vertices and t in vertices]
    ggraph = GGraph(list(vertices.values()), gedges)

    x_offset = 0.0
    for component in ggraph.C:
        sug = SugiyamaLayout(component)
        sug.xspace = 2
        sug.yspace = 2
        sug.init_all()
        sug.draw()
        min_x = min(v.view.xy[0] - v.view.w / 2 for v in component.sV)
        max_x = max(v.view.xy[0] + v.view.w / 2 for v in component.sV)
        for vertex in component.sV:
            x, y = vertex.view.xy
            vertex.view.xy = (x - min_x + x_offset, y)
        x_offset += (max_x - min_x) + X_SPACE

    boxes: dict[str, NodeBox] = {}
    for nid, vertex in vertices.items():
        cx, cy = vertex.view.xy
        type_name = graph.nodes[nid].type if nid in graph.nodes else ""
        w = _box_width(nid, type_name)
        x = int(round(cx - w / 2))
        y = int(round(cy - BOX_H / 2))
        boxes[nid] = NodeBox(node_id=nid, x=x, y=y, w=w, h=BOX_H)

    min_x = min(b.x for b in boxes.values())
    min_y = min(b.y for b in boxes.values())
    boxes = {nid: NodeBox(nid, b.x - min_x + 2, b.y - min_y + 1, b.w, b.h)
             for nid, b in boxes.items()}

    routes = []
    for source, target, kind, when in all_pairs:
        if source not in boxes or target not in boxes:
            continue
        back = boxes[target].y <= boxes[source].y
        routes.append(EdgeRoute(source, target, when, kind, back))

    width = max(b.x + b.w for b in boxes.values()) + 8
    height = max(b.y + b.h for b in boxes.values()) + 2
    return GraphLayout(boxes=boxes, edges=routes, width=width, height=height)
