"""probe_url: normalization + probing a typed endpoint."""

import httpx
import pytest
import respx

from graphx.llm.discovery import _normalize_url, probe_url

MODELS = {"object": "list", "data": [{"id": "qwen-big"}, {"id": "qwen-small"}]}


class TestNormalize:
    @pytest.mark.parametrize("raw,origin,host,port", [
        ("localhost:8000", "http://localhost:8000", "localhost", 8000),
        ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000", "127.0.0.1", 8000),
        ("127.0.0.1:8000/", "http://127.0.0.1:8000", "127.0.0.1", 8000),
        ("https://gw.example.com/v1", "https://gw.example.com", "gw.example.com", 443),
        ("http://box:11434", "http://box:11434", "box", 11434),
    ])
    def test_forms(self, raw, origin, host, port):
        assert _normalize_url(raw) == (origin, host, port)


class TestProbe:
    @respx.mock
    async def test_openai_endpoint(self):
        respx.get("http://10.0.0.7:8000/v1/models").mock(
            return_value=httpx.Response(200, json=MODELS))
        respx.get("http://10.0.0.7:8000/api/tags").mock(
            return_value=httpx.Response(404))
        ep = await probe_url("10.0.0.7:8000")
        assert ep.kind == "openai"
        assert ep.base_url == "http://10.0.0.7:8000/v1"
        assert ep.models == ("qwen-big", "qwen-small")

    @respx.mock
    async def test_https_endpoint_with_v1_suffix(self):
        respx.get("https://gw.example.com/v1/models").mock(
            return_value=httpx.Response(200, json=MODELS))
        respx.get("https://gw.example.com/api/tags").mock(
            return_value=httpx.Response(404))
        ep = await probe_url("https://gw.example.com/v1")
        assert ep.base_url == "https://gw.example.com/v1"
        assert ep.alias == "openai_gw_example_com_443"

    @respx.mock
    async def test_not_an_llm_server(self):
        respx.get("http://x:9/v1/models").mock(return_value=httpx.Response(200, json={"a": 1}))
        respx.get("http://x:9/api/tags").mock(return_value=httpx.Response(404))
        assert await probe_url("http://x:9") is None
