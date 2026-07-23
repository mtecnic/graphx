"""WorkflowDraft — the shared mutable workflow both engines build against.

Two backends behind one mutation API: a plain dict (generate) or a
comment-preserving WorkflowFile (edit). validate() is the single source
of truth (build_graph + validate_graph), so neither engine can produce
an invalid or ungrounded workflow.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..connectors.registry import ConnectorResult, get as get_connector
from ..model.graph import Graph
from ..model.validate import Issue, validate_graph
from ..model.yaml_loader import LoadError, build_graph
from ..model.yaml_writer import WorkflowFile
from ..nodes.registry import known_types
from ..secrets import find_secret_refs

_KEY_ORDER = ["version", "name", "description", "providers", "mcp_servers",
              "config", "state", "entry", "nodes", "edges"]


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Forgive common model quirks so a nearly-right workflow still loads:
    nodes/edges emitted as a dict-keyed-by-id → a list."""
    nodes = data.get("nodes")
    if isinstance(nodes, dict):
        data["nodes"] = [{"id": nid, **(spec or {})} for nid, spec in nodes.items()]
    edges = data.get("edges")
    if isinstance(edges, dict):
        data["edges"] = [{"from": src, **(spec or {})} for src, spec in edges.items()]
    entry = data.get("entry")
    if isinstance(entry, str):
        data["entry"] = [entry]
    return data


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class WorkflowDraft:
    def __init__(self, data: dict[str, Any], wf: WorkflowFile | None = None):
        self._data = data
        self._wf = wf              # set in edit mode (mutations delegate to it)

    # ----------------------------------------------------------- factories

    @classmethod
    def blank(cls, name: str, description: str = "") -> WorkflowDraft:
        return cls({"version": 1, "name": name, "description": description,
                    "state": {}, "entry": [], "nodes": [], "edges": []})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowDraft:
        if not isinstance(data, dict):
            raise LoadError("workflow must be a mapping/object")
        data = _normalize(dict(data))
        data.setdefault("version", 1)
        return cls(data)

    @classmethod
    def from_file(cls, path: str | Path) -> WorkflowDraft:
        wf = WorkflowFile(path)
        return cls(dict(wf.data), wf=wf)

    # --------------------------------------------------------------- edit

    @property
    def _nodes(self) -> list:
        return self._data.setdefault("nodes", [])

    def _target(self):
        """The object mutations apply to (WorkflowFile in edit mode)."""
        return self._wf if self._wf is not None else self

    def add_node(self, node: dict, after: str | None = None) -> None:
        if "id" not in node or "type" not in node:
            raise LoadError("a node needs at least 'id' and 'type'")
        if self._wf is not None:
            self._wf.add_node(node, after=after)
            return
        if any(n.get("id") == node["id"] for n in self._nodes):
            raise LoadError(f"node id '{node['id']}' already exists")
        self._nodes.append(node)

    def remove_node(self, node_id: str) -> None:
        if self._wf is not None:
            self._wf.remove_node(node_id)
            return
        self._data["nodes"] = [n for n in self._nodes if n.get("id") != node_id]
        self._data["edges"] = [e for e in self._data.get("edges", [])
                               if node_id not in (e.get("from"), e.get("to"))]

    def update_node(self, node_id: str, changes: dict) -> None:
        if self._wf is not None:
            self._wf.update_node(node_id, changes)
            return
        for node in self._nodes:
            if node.get("id") == node_id:
                for key, value in changes.items():
                    if value is None:          # match WorkflowFile: None deletes the key
                        node.pop(key, None)
                    else:
                        node[key] = value
                return
        raise LoadError(f"no node '{node_id}'")

    def add_edge(self, source: str, target: str, when: str | None = None) -> None:
        if self._wf is not None:
            self._wf.add_edge(source, target, when)
            return
        edge: dict[str, Any] = {"from": source, "to": target}
        if when:
            edge["when"] = when
        self._data.setdefault("edges", []).append(edge)

    def remove_edge(self, source: str, target: str) -> None:
        if self._wf is not None:
            self._wf.remove_edge(source, target)
            return
        self._data["edges"] = [e for e in self._data.get("edges", [])
                               if (e.get("from"), e.get("to")) != (source, target)]

    def set_state(self, key: str, spec: dict) -> None:
        if self._wf is not None:
            self._wf.data.setdefault("state", {})[key] = spec
            return
        self._data.setdefault("state", {})[key] = spec

    def set_entry(self, ids: list[str]) -> None:
        if self._wf is not None:
            self._wf.data["entry"] = ids
            return
        self._data["entry"] = ids

    def set_provider(self, alias: str, cfg: dict) -> None:
        if self._wf is not None:
            self._wf.set_provider(alias, cfg)
            return
        self._data.setdefault("providers", {})[alias] = cfg

    def set_mcp_server(self, name: str, cfg: dict) -> None:
        if self._wf is not None:
            self._wf.set_mcp_server(name, cfg)
            return
        self._data.setdefault("mcp_servers", {})[name] = cfg

    def add_connector(self, key: str, node_id: str, values: dict,
                      after: str | None = None) -> ConnectorResult:
        result = get_connector(key).render(node_id, values)   # KeyError on unknown key
        self.add_node(result.node, after=after)
        if result.mcp_servers:
            for name, cfg in result.mcp_servers.items():
                self.set_mcp_server(name, cfg)
        return result

    # ------------------------------------------------------- read / check

    def to_yaml(self) -> str:
        if self._wf is not None:
            return self._wf.dumps()
        buffer = io.StringIO()
        ordered = {k: self._data[k] for k in _KEY_ORDER if k in self._data}
        ordered.update({k: v for k, v in self._data.items() if k not in _KEY_ORDER})
        _yaml().dump(ordered, buffer)
        return buffer.getvalue()

    def _raw(self) -> dict:
        return dict(self._wf.data) if self._wf is not None else self._data

    def build(self) -> Graph:
        return build_graph(self._raw())

    def validate(self) -> list[Issue]:
        try:
            graph = self.build()
        except LoadError as exc:
            return [Issue("error", "structure", str(exc))]
        return validate_graph(graph, known_types=known_types())

    def secret_refs(self) -> set[str]:
        return find_secret_refs(self._raw())
