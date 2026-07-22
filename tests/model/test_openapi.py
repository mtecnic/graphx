"""OpenAPI auto-fill: parsing, scaffolding, fetching, CLI, and dogfooding."""

from pathlib import Path

import httpx
import pytest
import respx

from graphx.model.openapi import (
    fetch_spec, parse_operations, scaffold_api_node, suggest_outputs,
)

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "petstore", "version": "1"},
    "servers": [{"url": "/api"}],
    "paths": {
        "/pets/{pet_id}": {
            "get": {
                "summary": "Get a pet",
                "operationId": "get_pet",
                "parameters": [
                    {"name": "pet_id", "in": "path", "required": True,
                     "schema": {"type": "integer"}},
                    {"name": "verbose", "in": "query", "required": False,
                     "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Pet"}}}}},
            },
        },
        "/pets": {
            "post": {
                "summary": "Create a pet",
                "operationId": "create_pet",
                "parameters": [
                    {"name": "x-token", "in": "header", "required": True,
                     "schema": {"type": "string"}},
                ],
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/NewPet"}}}},
                "responses": {"201": {"content": {"application/json": {
                    "schema": {"type": "object", "properties": {
                        "id": {"type": "integer"},
                        "tags": {"type": "array",
                                 "items": {"type": "object", "properties": {
                                     "label": {"type": "string"}}}},
                    }}}}}},
            },
        },
    },
    "components": {"schemas": {
        "Pet": {"type": "object", "properties": {
            "name": {"type": "string"},
            "owner": {"type": "object", "properties": {"email": {"type": "string"}}},
        }},
        "NewPet": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "nickname": {"type": "string"}}},
    }},
}


class TestParse:
    def test_operations_and_params(self):
        ops = {f"{o.method} {o.path}": o for o in parse_operations(SPEC)}
        get_pet = ops["get /pets/{pet_id}"]
        assert get_pet.operation_id == "get_pet"
        path_param = next(p for p in get_pet.params if p.location == "path")
        assert path_param.name == "pet_id" and path_param.required
        assert get_pet.response["properties"]["name"]["type"] == "string"  # $ref resolved

    def test_request_body_deref(self):
        create = next(o for o in parse_operations(SPEC) if o.method == "post")
        assert create.request_body["required"] == ["name"]


class TestScaffold:
    def test_get_scaffold(self):
        op = next(o for o in parse_operations(SPEC) if o.operation_id == "get_pet")
        node = scaffold_api_node(op, "http://localhost:9000/api", SPEC)
        assert node["id"] == "get_pet"
        assert node["method"] == "GET"
        assert node["url"] == "http://localhost:9000/api/pets/<state.pet_id>"
        assert "params" not in node                      # optional query skipped
        assert node["output"]["name"] == "$.name"
        assert node["output"]["email"] == "$.owner.email"

    def test_post_scaffold_body_and_headers(self):
        op = next(o for o in parse_operations(SPEC) if o.operation_id == "create_pet")
        node = scaffold_api_node(op, "http://x/api", SPEC, node_id="make_pet")
        assert node["id"] == "make_pet"
        assert node["json_body"] == {"name": "<state.name>",
                                     "nickname": "<state.nickname>"}
        assert node["headers"] == {"x-token": "<state.x-token>"}
        assert node["output"]["label"] == "$.tags[0].label"

    def test_suggest_outputs_caps(self):
        big = {"type": "object", "properties": {
            f"f{i}": {"type": "string"} for i in range(30)}}
        assert len(suggest_outputs(big)) <= 8


class TestFetch:
    @respx.mock
    async def test_probes_openapi_json(self):
        respx.get("http://svc.test/openapi.json").mock(
            return_value=httpx.Response(200, json=SPEC))
        spec, base = await fetch_spec("http://svc.test")
        assert spec["info"]["title"] == "petstore"
        assert base == "http://svc.test/api"             # servers[0] joined with origin

    @respx.mock
    async def test_error_when_nothing_found(self):
        from graphx.model.openapi import SpecError
        for candidate in ("openapi.json", "openapi.yaml", "swagger.json",
                          "api/openapi.json", "v1/openapi.json"):
            respx.get(f"http://svc.test/{candidate}").mock(
                return_value=httpx.Response(404))
        with pytest.raises(SpecError):
            await fetch_spec("http://svc.test")


class TestDogfood:
    def test_scaffold_from_graphx_own_server_spec(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from graphx.server.app import create_app

        spec = create_app(".").openapi()
        ops = parse_operations(spec)
        status_op = next(o for o in ops if o.path == "/runs/{thread_id}"
                         and o.method == "get")
        node = scaffold_api_node(status_op, "http://localhost:8420", spec)
        assert node["url"] == "http://localhost:8420/runs/<state.thread_id>"
        assert node["method"] == "GET"


class TestCli:
    @respx.mock
    def test_scaffold_api_command_adds_node(self, tmp_path: Path):
        from typer.testing import CliRunner

        from graphx.cli import app as cli_app
        from graphx.model.yaml_loader import load_graph

        respx.get("http://svc.test/openapi.json").mock(
            return_value=httpx.Response(200, json=SPEC))
        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "version: 1\nname: t\nentry: [a]\n"
            "nodes:\n  - { id: a, type: function, handler: 'graphx.demo:sysinfo' }\n"
            "edges:\n  - { from: a, to: end }\n")

        runner = CliRunner()
        result = runner.invoke(cli_app, ["scaffold-api", str(wf), "http://svc.test",
                                         "--op", "GET /pets/{pet_id}"])
        assert result.exit_code == 0, result.output
        graph = load_graph(wf)
        assert "get_pet" in graph.nodes
        assert graph.nodes["get_pet"].config["url"].endswith("/pets/<state.pet_id>")

    @respx.mock
    def test_scaffold_api_lists_operations(self, tmp_path: Path):
        from typer.testing import CliRunner

        from graphx.cli import app as cli_app

        respx.get("http://svc.test/openapi.json").mock(
            return_value=httpx.Response(200, json=SPEC))
        wf = tmp_path / "wf.yaml"
        wf.write_text(
            "version: 1\nname: t\nentry: [a]\n"
            "nodes:\n  - { id: a, type: function, handler: 'graphx.demo:sysinfo' }\n")

        runner = CliRunner()
        result = runner.invoke(cli_app, ["scaffold-api", str(wf), "http://svc.test"])
        assert result.exit_code == 0, result.output
        assert "GET /pets/{pet_id}" in result.output
        assert "POST /pets" in result.output
