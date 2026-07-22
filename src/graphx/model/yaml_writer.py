"""Structured mutations that write back to the workflow YAML.

ruamel round-trip mode preserves comments, key order, and block
formatting, so palette edits produce reviewable diffs — the file stays
the source of truth. Known cosmetic caveat: spacing inside flow-style
maps is normalized (`{ type: str }` → `{type: str}`) on first write;
ruamel exposes no knob for it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .yaml_loader import LoadError


class WorkflowFile:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.yaml = YAML()  # round-trip mode
        self.yaml.preserve_quotes = True
        self.yaml.width = 100
        # match the prevailing hand-written style so diffs stay minimal
        self.yaml.indent(mapping=2, sequence=4, offset=2)
        self.data = self.yaml.load(self.path.read_text())
        if not isinstance(self.data, CommentedMap):
            raise LoadError(f"{path}: top level must be a mapping")

    # ------------------------------------------------------------- queries

    def node_ids(self) -> list[str]:
        return [n.get("id") for n in self.data.get("nodes") or []]

    def _node_index(self, node_id: str) -> int:
        for i, node in enumerate(self.data.get("nodes") or []):
            if node.get("id") == node_id:
                return i
        raise LoadError(f"no node '{node_id}' in {self.path}")

    # ----------------------------------------------------------- mutations

    def add_node(self, node: dict[str, Any], after: str | None = None) -> None:
        if "id" not in node or "type" not in node:
            raise LoadError("new node needs at least 'id' and 'type'")
        if node["id"] in self.node_ids():
            raise LoadError(f"node id '{node['id']}' already exists")
        nodes = self.data.setdefault("nodes", [])
        index = self._node_index(after) + 1 if after else len(nodes)
        nodes.insert(index, node)

    def remove_node(self, node_id: str) -> None:
        nodes = self.data.get("nodes") or []
        nodes.pop(self._node_index(node_id))
        edges = self.data.get("edges") or []
        self.data["edges"] = [e for e in edges
                              if e.get("from") != node_id and e.get("to") != node_id]

    def update_node(self, node_id: str, changes: dict[str, Any]) -> None:
        node = (self.data.get("nodes") or [])[self._node_index(node_id)]
        for key, value in changes.items():
            if value is None:
                node.pop(key, None)
            else:
                node[key] = value

    def set_provider(self, alias: str, config: dict[str, Any]) -> None:
        """Create or update a providers: entry (idempotent)."""
        providers = self.data.get("providers")
        if providers is None:
            providers = {}
            # keep providers near the top, right after version/name/description
            index = 0
            for key in self.data:
                if key not in ("version", "name", "description"):
                    break
                index += 1
            self.data.insert(index, "providers", providers)
        providers[alias] = config

    def add_edge(self, source: str, target: str, when: str | None = None) -> None:
        edges = self.data.setdefault("edges", [])
        for edge in edges:
            if edge.get("from") == source and edge.get("to") == target \
                    and edge.get("when") == when:
                raise LoadError(f"edge {source}->{target} already exists")
        edge: dict[str, Any] = {"from": source, "to": target}
        if when:
            edge["when"] = when
        edges.append(edge)

    def remove_edge(self, source: str, target: str) -> None:
        edges = self.data.get("edges") or []
        kept = [e for e in edges if not (e.get("from") == source and e.get("to") == target)]
        if len(kept) == len(edges):
            raise LoadError(f"no edge {source}->{target} in {self.path}")
        self.data["edges"] = kept

    # ---------------------------------------------------------------- save

    def dumps(self) -> str:
        buffer = io.StringIO()
        self.yaml.dump(self.data, buffer)
        return buffer.getvalue()

    def save(self) -> None:
        self.path.write_text(self.dumps())
