"""Edit mode: oneshot rewrite + agentic surgical edit."""

import json

from graphx.builder.agentic import edit_agentic
from graphx.builder.catalog import build_catalog
from graphx.builder.oneshot import edit_oneshot
from graphx.llm.client import LLMResponse, ToolCall, Usage
from graphx.nodes.registry import load_builtin_nodes

from conftest import ScriptedLLM

load_builtin_nodes()
CAT = build_catalog(None)

CURRENT = """\
version: 1
name: base
entry: [start]
nodes:
  - id: start
    type: shell
    command: ["echo", "hi"]
edges:
  - { from: start, to: end }
"""


async def test_edit_oneshot_emits_revised_workflow():
    revised = {
        "version": 1, "name": "base", "entry": ["start"],
        "nodes": [
            {"id": "start", "type": "shell", "command": ["echo", "hi"]},
            {"id": "ping", "type": "shell", "command": ["echo", "done"]},
        ],
        "edges": [{"from": "start", "to": "ping"}, {"from": "ping", "to": "end"}],
    }
    llm = ScriptedLLM([json.dumps(revised)])
    result = await edit_oneshot(llm, "local/m", CAT, CURRENT, "add a final echo 'done' step")
    assert result.ok
    assert "ping" in result.yaml
    # the instruction + current workflow were both in the prompt
    user_msg = llm.calls[0]["messages"][-1]["content"]
    assert "add a final echo" in user_msg and "start" in user_msg


async def test_edit_agentic_slack_case(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    monkeypatch.setattr("graphx.secrets._keyring", lambda: None)
    wf = tmp_path / "base.yaml"
    wf.write_text(CURRENT)

    def calls(*tc):
        return LLMResponse(text="", usage=Usage(5, 5), model="fake",
                           tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a)
                                       for i, (n, a) in enumerate(tc)])

    llm = ScriptedLLM([
        calls(("add_connector", {"key": "slack", "id": "alert",
                                 "values": {"message": "workflow done"}}),
              ("add_edge", {"from": "start", "to": "alert"}),
              ("add_edge", {"from": "alert", "to": "end"})),
        calls(("finish", {})),
    ])
    result = await edit_agentic(llm, "local/m", CAT, wf,
                                "add a Slack alert after start")
    assert result.ok
    # the draft mutated the WorkflowFile backend (comment-preserving)
    assert "slack_webhook_url" in result.draft.secret_refs()
    assert "alert" in result.yaml
