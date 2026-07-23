"""Agentic engine: the model drives builder tools to construct the graph.

Mirrors the tool-call loop in nodes/agent.py. Every mutating tool is
grounded (unknown node types / connectors are rejected and the error is
fed back), and `finish` refuses while the validator reports errors — so
the loop can only end on a valid workflow (or hit max_rounds).
"""

from __future__ import annotations

import json

from ..llm.client import ToolDef
from ..model.validate import Issue, has_errors
from ..model.yaml_loader import LoadError
from ..nodes.registry import known_types
from .catalog import Catalog
from .draft import WorkflowDraft
from .prompts import AGENTIC_SYSTEM
from .result import BuildResult

_OBJ = {"type": "object", "additionalProperties": True}

BUILDER_TOOLS = [
    ToolDef("set_state", "Declare a state channel.",
            {"type": "object", "properties": {
                "key": {"type": "string"},
                "type": {"type": "string"}, "reducer": {"type": "string"},
                "default": {}}, "required": ["key"]}),
    ToolDef("set_entry", "Set the workflow's start node id(s).",
            {"type": "object", "properties": {
                "ids": {"type": "array", "items": {"type": "string"}}},
             "required": ["ids"]}),
    ToolDef("add_node", "Add a node. 'type' must be a known node type; 'config' holds "
                        "that type's fields.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "type": {"type": "string"},
                "config": _OBJ, "after": {"type": "string"}},
             "required": ["id", "type"]}),
    ToolDef("update_node", "Change fields of an existing node.",
            {"type": "object", "properties": {
                "id": {"type": "string"}, "changes": _OBJ}, "required": ["id", "changes"]}),
    ToolDef("remove_node", "Remove a node and its edges.",
            {"type": "object", "properties": {"id": {"type": "string"}},
             "required": ["id"]}),
    ToolDef("add_edge", "Connect two nodes; optional 'when' boolean expression.",
            {"type": "object", "properties": {
                "from": {"type": "string"}, "to": {"type": "string"},
                "when": {"type": "string"}}, "required": ["from", "to"]}),
    ToolDef("remove_edge", "Remove an edge.",
            {"type": "object", "properties": {
                "from": {"type": "string"}, "to": {"type": "string"}},
             "required": ["from", "to"]}),
    ToolDef("add_connector", "Add a credential-wired preset node by connector key.",
            {"type": "object", "properties": {
                "key": {"type": "string"}, "id": {"type": "string"}, "values": _OBJ},
             "required": ["key", "id"]}),
    ToolDef("describe_type", "Get the full config schema for a node type.",
            {"type": "object", "properties": {"type": {"type": "string"}},
             "required": ["type"]}),
    ToolDef("validate", "Static-check the workflow so far; returns errors and warnings.",
            {"type": "object", "properties": {}}),
    ToolDef("finish", "Signal the workflow is complete (only succeeds if it validates).",
            {"type": "object", "properties": {}}),
]


def _fmt(issues: list[Issue]) -> str:
    if not issues:
        return "no issues."
    return "\n".join(f"- [{i.severity}] {i.where}: {i.message}" for i in issues)


def _make_dispatch(draft: WorkflowDraft, catalog: Catalog):
    def add_node(a):
        if a.get("type") not in known_types():
            return f"error: unknown node type '{a.get('type')}' " \
                   f"(have: {', '.join(sorted(known_types()))})"
        node = {"id": a["id"], "type": a["type"], **(a.get("config") or {})}
        draft.add_node(node, after=a.get("after"))
        return f"added node '{a['id']}'"

    def add_connector(a):
        result = draft.add_connector(a["key"], a.get("id") or a["key"],
                                     a.get("values") or {})
        secrets = f" (needs secret: {', '.join(result.secret_names)})" \
            if result.secret_names else ""
        return f"added connector '{a['key']}' as node '{a.get('id') or a['key']}'{secrets}"

    def describe_type(a):
        schema = catalog.schema_for(a.get("type"))
        return json.dumps(schema.get("properties", {}))[:800] if schema \
            else f"error: unknown type '{a.get('type')}'"

    return {
        "set_state": lambda a: (draft.set_state(
            a["key"], {k: a[k] for k in ("type", "reducer", "default") if k in a}),
            f"state '{a['key']}' set")[1],
        "set_entry": lambda a: (draft.set_entry(a["ids"]), f"entry = {a['ids']}")[1],
        "add_node": add_node,
        "update_node": lambda a: (draft.update_node(a["id"], a["changes"]),
                                  f"updated '{a['id']}'")[1],
        "remove_node": lambda a: (draft.remove_node(a["id"]), f"removed '{a['id']}'")[1],
        "add_edge": lambda a: (draft.add_edge(a["from"], a["to"], a.get("when")),
                               f"edge {a['from']}->{a['to']}")[1],
        "remove_edge": lambda a: (draft.remove_edge(a["from"], a["to"]),
                                  f"removed edge {a['from']}->{a['to']}")[1],
        "add_connector": add_connector,
        "describe_type": describe_type,
        "validate": lambda a: _fmt(draft.validate()),
    }


async def _run(llm, model: str, draft: WorkflowDraft, catalog: Catalog,
               task_msg: str, *, max_rounds: int, temperature: float) -> BuildResult:
    messages = [{"role": "system", "content": AGENTIC_SYSTEM + "\n\n" + catalog.render()},
                {"role": "user", "content": task_msg}]
    dispatch = _make_dispatch(draft, catalog)
    usage_total = 0
    finished = False

    resp = await llm.chat(model, messages, tools=BUILDER_TOOLS, temperature=temperature)
    usage_total += resp.usage.total
    rounds = 0
    while resp.tool_calls and not finished and rounds < max_rounds:
        rounds += 1
        messages.append({"role": "assistant", "content": resp.text or None,
                         "tool_calls": [{"id": c.id, "type": "function", "function": {
                             "name": c.name, "arguments": json.dumps(c.arguments)}}
                             for c in resp.tool_calls]})
        for call in resp.tool_calls:
            if call.name == "finish":
                issues = draft.validate()
                if has_errors(issues):
                    text = "NOT finished — fix these errors first:\n" + _fmt(issues)
                else:
                    finished = True
                    text = "finished; the workflow is valid."
            else:
                handler = dispatch.get(call.name)
                if handler is None:
                    text = f"error: unknown tool '{call.name}'"
                else:
                    try:
                        text = handler(call.arguments)
                    except (LoadError, KeyError, ValueError) as exc:
                        text = f"error: {exc}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
        if finished:
            break
        resp = await llm.chat(model, messages, tools=BUILDER_TOOLS,
                              temperature=temperature)
        usage_total += resp.usage.total

    issues = draft.validate()
    return BuildResult(ok=not has_errors(issues),
                       exhausted=(rounds >= max_rounds and not finished),
                       yaml=draft.to_yaml(), draft=draft, issues=issues,
                       tokens=usage_total, engine="agentic", model=model)


async def generate_agentic(llm, model: str, catalog: Catalog, description: str, *,
                           name: str = "generated", max_rounds: int = 12,
                           temperature: float = 0.2, seed_provider=None) -> BuildResult:
    draft = WorkflowDraft.blank(name, description[:120])
    if seed_provider:
        draft.set_provider(*seed_provider)
    task = (f"Build a workflow for this request:\n{description}\n"
            "Use the tools to add state, nodes, and edges, then call finish.")
    return await _run(llm, model, draft, catalog, task,
                      max_rounds=max_rounds, temperature=temperature)


async def edit_agentic(llm, model: str, catalog: Catalog, path, instruction: str, *,
                       max_rounds: int = 12, temperature: float = 0.2) -> BuildResult:
    draft = WorkflowDraft.from_file(path)
    task = (f"Here is the current workflow:\n```yaml\n{draft.to_yaml()}\n```\n\n"
            f"Apply this change with the tools, then call finish:\n{instruction}")
    return await _run(llm, model, draft, catalog, task,
                      max_rounds=max_rounds, temperature=temperature)
