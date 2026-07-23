"""Capability catalog — the grounding context handed to the model.

Two-tier: a condensed always-on card (node one-liners + connectors +
providers + syntax) kept small for local context windows, plus full
per-type JSON schemas available on demand (repair hints / describe_type).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..connectors.registry import all_connectors
from ..nodes.registry import get_node_type, known_types, load_builtin_nodes

# Hand-authored one-line purpose per node type (the schema gives the fields).
_PURPOSE: dict[str, str] = {
    "agent": "LLM call: reasons over a prompt, optional tools, optional JSON output_schema",
    "api": "HTTP request to a URL; extract response fields with $.json.paths into output",
    "mcp": "call one tool on a configured MCP server",
    "function": "call a Python function 'module:name' with args",
    "shell": "run a shell command / CLI agent; captures stdout",
    "condition": "branch (and loop) on expressions; each branch goes to a node",
    "router": "an LLM picks which downstream node to go to",
    "map": "run an inline node once per item of a list (fan-out), collect results",
    "merge": "barrier join: wait for parallel branches, proceed on a success threshold",
    "human": "pause for human approval/input (interrupt), resume with an answer",
    "wait": "sleep for a duration",
    "subworkflow": "run another workflow file as one node",
}

_SYNTAX_CARD = """\
SYNTAX:
- Reference another node's output: <node_id.field>  (e.g. <fetch.json>, <write.draft>)
- Reference a state channel: <state.key>   ; a map item: <item.x>
- A credential is written secret://NAME (never a literal key); the user is prompted for it.
- Edges: {from: A, to: B} runs B after A. Optional condition: {from: A, to: B, when: "score >= 0.8"}.
  'when' is a boolean expression over state channels and node outputs (==, <, >, and, or, not, in).
- 'entry' lists the node id(s) that start the workflow. 'end' is the implicit terminal target.
- Top-level keys, in order: version (=1), name, description, providers, state, entry, nodes, edges.
- state channels: {type: str|int|float|bool|list|dict, reducer: last|append|extend|sum|merge_dict, default: ...}
- A node may add: retry:{attempts}, timeout: 30s, updates:{state_key: "<self.field>"} to write state."""


@dataclass(frozen=True)
class Catalog:
    node_lines: dict[str, str]
    schemas: dict[str, dict]
    connector_lines: list[str]
    providers_block: str
    syntax_card: str = _SYNTAX_CARD
    default_model: str | None = None
    _rendered: dict = field(default_factory=dict, compare=False)

    def schema_for(self, type_: str) -> dict | None:
        return self.schemas.get(type_)

    def render(self) -> str:
        parts = ["You build workflows for graphx. NODE TYPES (use only these):"]
        parts += [f"- {line}" for line in self.node_lines.values()]
        parts.append("\nCONNECTORS (credential-wired preset nodes; add via a connector, "
                     "or as a plain node):")
        parts += [f"- {line}" for line in self.connector_lines]
        if self.providers_block:
            parts.append("\nAVAILABLE MODELS (use one of these exact strings for any "
                         "agent/router 'model'):")
            parts.append(self.providers_block)
        parts.append("\n" + self.syntax_card)
        return "\n".join(parts)


def _node_line(type_: str) -> str:
    schema = get_node_type(type_).config_model.model_json_schema()
    props = list((schema.get("properties") or {}).keys())
    required = schema.get("required") or []
    optional = [p for p in props if p not in required]
    purpose = _PURPOSE.get(type_, "")
    bits = f"{type_} — {purpose}"
    if required:
        bits += f"  [required: {', '.join(required)}]"
    if optional:
        bits += f"  [optional: {', '.join(optional[:6])}]"
    return bits


def build_catalog(endpoints: list | None = None) -> Catalog:
    load_builtin_nodes()
    types = sorted(known_types())
    node_lines = {t: _node_line(t) for t in types}
    schemas = {t: get_node_type(t).config_model.model_json_schema() for t in types}

    connector_lines = []
    for c in all_connectors():
        fields = ", ".join(f.name for f in c.fields if f.required and f.default is None)
        secrets = ", ".join(s.name for s in c.secrets)
        line = f"{c.key} ({c.category}) — {c.description}"
        if fields:
            line += f"  fields: {fields}"
        if secrets:
            line += f"  secrets: {secrets}"
        connector_lines.append(line)

    providers_block = ""
    default_model = None
    if endpoints:
        from ..llm.discovery import best_endpoint
        lines = []
        best = best_endpoint(endpoints)
        for ep in endpoints:
            for model in ep.models:
                mark = "  (recommended)" if best and ep is best and \
                    model == ep.models[0] else ""
                lines.append(f"  {ep.alias}/{model}{mark}")
        providers_block = "\n".join(lines)
        if best and best.models:
            default_model = f"{best.alias}/{best.models[0]}"

    return Catalog(node_lines=node_lines, schemas=schemas,
                   connector_lines=connector_lines, providers_block=providers_block,
                   default_model=default_model)
