"""Capability catalog: completeness, grounding, token budget."""

import time

from graphx.builder.catalog import build_catalog
from graphx.connectors.registry import all_connectors
from graphx.llm.discovery import Endpoint
from graphx.nodes.registry import get_node_type, known_types, load_builtin_nodes

load_builtin_nodes()


def test_every_node_type_present():
    cat = build_catalog(None)
    for t in known_types():
        assert t in cat.node_lines
        assert cat.schema_for(t) == get_node_type(t).config_model.model_json_schema()


def test_every_connector_present():
    cat = build_catalog(None)
    text = "\n".join(cat.connector_lines)
    for c in all_connectors():
        assert c.key in text


def test_render_within_token_budget():
    cat = build_catalog(None)
    rendered = cat.render()
    # rough token estimate (chars/4) should stay small for local context windows
    assert len(rendered) / 4 < 1600, f"catalog too big: ~{len(rendered)//4} tokens"
    assert "SYNTAX" in rendered
    assert "secret://" in rendered


def test_providers_block_and_default_model():
    ep = Endpoint(base_url="http://127.0.0.1:8000/v1", kind="openai",
                  host="127.0.0.1", port=8000, models=("big", "small"),
                  checked_at=time.time())
    cat = build_catalog([ep])
    assert "openai_local_8000/big" in cat.providers_block
    assert "recommended" in cat.providers_block
    assert cat.default_model == "openai_local_8000/big"
