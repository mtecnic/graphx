"""Agentic engine: tool-driven build, grounding, finish gating."""

from graphx.builder.agentic import generate_agentic
from graphx.builder.catalog import build_catalog
from graphx.llm.client import LLMResponse, ToolCall, Usage
from graphx.nodes.registry import load_builtin_nodes

from conftest import ScriptedLLM

load_builtin_nodes()
CAT = build_catalog(None)


def calls(*tool_calls) -> LLMResponse:
    return LLMResponse(text="", usage=Usage(10, 10), model="fake",
                       tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a)
                                   for i, (n, a) in enumerate(tool_calls)])


async def test_builds_a_graph_via_tools():
    llm = ScriptedLLM([
        calls(("add_node", {"id": "a", "type": "shell",
                            "config": {"command": ["echo", "hi"]}}),
              ("set_entry", {"ids": ["a"]}),
              ("add_edge", {"from": "a", "to": "end"})),
        calls(("validate", {})),
        calls(("finish", {})),
    ])
    result = await generate_agentic(llm, "local/m", CAT, "echo hi")
    assert result.ok
    assert "- id: a" in result.yaml
    assert "echo" in result.yaml


async def test_unknown_type_is_rejected_then_corrected():
    llm = ScriptedLLM([
        calls(("add_node", {"id": "a", "type": "bogus", "config": {}})),
        calls(("add_node", {"id": "a", "type": "shell",
                            "config": {"command": ["echo", "hi"]}}),
              ("set_entry", {"ids": ["a"]}),
              ("add_edge", {"from": "a", "to": "end"})),
        calls(("finish", {})),
    ])
    result = await generate_agentic(llm, "local/m", CAT, "echo hi")
    assert result.ok
    # the first (bogus) tool call got an error message fed back
    tool_msgs = [m for call in llm.calls for m in call["messages"]
                 if m.get("role") == "tool"]
    assert any("unknown node type" in m["content"] for m in tool_msgs)


async def test_finish_refuses_while_invalid():
    # finish called before any nodes exist → refused, loop continues, then builds
    llm = ScriptedLLM([
        calls(("finish", {})),
        calls(("add_node", {"id": "a", "type": "shell",
                            "config": {"command": ["echo", "x"]}}),
              ("set_entry", {"ids": ["a"]}),
              ("add_edge", {"from": "a", "to": "end"})),
        calls(("finish", {})),
    ])
    result = await generate_agentic(llm, "local/m", CAT, "x")
    assert result.ok
    tool_msgs = [m for call in llm.calls for m in call["messages"]
                 if m.get("role") == "tool"]
    assert any("NOT finished" in m["content"] for m in tool_msgs)


async def test_add_connector_via_tool():
    llm = ScriptedLLM([
        calls(("add_node", {"id": "start", "type": "shell",
                            "config": {"command": ["echo", "go"]}}),
              ("add_connector", {"key": "slack", "id": "notify",
                                 "values": {"message": "done"}}),
              ("set_entry", {"ids": ["start"]}),
              ("add_edge", {"from": "start", "to": "notify"}),
              ("add_edge", {"from": "notify", "to": "end"})),
        calls(("finish", {})),
    ])
    result = await generate_agentic(llm, "local/m", CAT, "notify slack after echo")
    assert result.ok
    assert "secret://slack_webhook_url" in result.yaml
    assert "slack_webhook_url" in result.draft.secret_refs()
