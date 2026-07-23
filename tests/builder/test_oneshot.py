"""One-shot engine: happy path, repair loop, normalization, exhaustion."""

import json

from graphx.builder.catalog import build_catalog
from graphx.builder.oneshot import generate_oneshot
from graphx.model.yaml_loader import build_graph
from graphx.nodes.registry import load_builtin_nodes
from ruamel.yaml import YAML

from conftest import ScriptedLLM

load_builtin_nodes()
CAT = build_catalog(None)

VALID = {
    "version": 1, "name": "t", "state": {"x": {"type": "str", "default": ""}},
    "entry": ["a"],
    "nodes": [{"id": "a", "type": "shell", "command": ["echo", "hi"],
               "updates": {"x": "<self.stdout>"}}],
    "edges": [{"from": "a", "to": "end"}],
}


def _loads(yaml_text: str) -> dict:
    return YAML(typ="safe", pure=True).load(yaml_text)


async def test_happy_path_one_call():
    llm = ScriptedLLM([json.dumps(VALID)])
    result = await generate_oneshot(llm, "local/m", CAT, "echo hi")
    assert result.ok
    assert len(llm.calls) == 1
    build_graph(_loads(result.yaml))          # loads without error


async def test_repair_after_bad_attempt():
    bad = json.dumps({**VALID, "nodes": [{"id": "a", "type": "nonexistent"}]})
    llm = ScriptedLLM([bad, json.dumps(VALID)])
    result = await generate_oneshot(llm, "local/m", CAT, "echo hi", max_repairs=2)
    assert result.ok
    assert len(llm.calls) == 2
    # the repair message fed back the error + a schema hint
    repair_msg = llm.calls[1]["messages"][-1]["content"]
    assert "nonexistent" in repair_msg
    assert "corrected JSON" in repair_msg


async def test_normalizes_dict_nodes():
    # model emits nodes as a dict-keyed-by-id (common mistake) → still valid
    dict_form = {**VALID, "nodes": {"a": {"type": "shell", "command": ["echo", "hi"]}}}
    llm = ScriptedLLM([json.dumps(dict_form)])
    result = await generate_oneshot(llm, "local/m", CAT, "echo hi")
    assert result.ok
    assert "- id: a" in result.yaml            # rendered as a list


async def test_exhaustion_returns_best_attempt():
    bad = json.dumps({**VALID, "nodes": [{"id": "a", "type": "ghost"}]})
    llm = ScriptedLLM([bad, bad, bad, bad])
    result = await generate_oneshot(llm, "local/m", CAT, "x", max_repairs=2)
    assert not result.ok
    assert result.exhausted
    assert result.errors                       # flagged for manual fix
    assert result.yaml                          # still returns the best attempt


async def test_provider_seed_added():
    llm = ScriptedLLM([json.dumps(VALID)])
    result = await generate_oneshot(llm, "local/m", CAT, "x",
                                    seed_provider=("myprov", {"base_url": "http://x/v1"}))
    assert "myprov" in _loads(result.yaml).get("providers", {})
