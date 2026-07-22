"""Endpoint discovery: probe shapes, cache, scan, best-endpoint choice."""

import asyncio
import json
import time

import httpx
import pytest
import respx

from graphx.llm.discovery import (
    Endpoint, best_endpoint, discover, load_cache, probe, save_cache, scan,
)

OPENAI_MODELS = {"object": "list", "data": [{"id": "qwen-big"}, {"id": "qwen-small"}]}
OLLAMA_TAGS = {"models": [{"name": "llama3.2:3b"}, {"name": "phi4"}]}


async def _probe(host: str, port: int):
    async with httpx.AsyncClient() as client:
        return await probe(host, port, client, asyncio.Semaphore(5))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPHX_HOME", str(tmp_path))
    monkeypatch.setenv("GRAPHX_NO_LAN_SCAN", "1")


class TestProbe:
    @respx.mock
    async def test_openai_compatible_server(self):
        respx.get("http://10.0.0.5:8000/v1/models").mock(
            return_value=httpx.Response(200, json=OPENAI_MODELS))
        respx.get("http://10.0.0.5:8000/api/tags").mock(
            return_value=httpx.Response(404))
        endpoint = await _probe("10.0.0.5", 8000)
        assert endpoint.kind == "openai"
        assert endpoint.base_url == "http://10.0.0.5:8000/v1"
        assert endpoint.models == ("qwen-big", "qwen-small")
        assert endpoint.alias == "openai_10_0_0_5_8000"

    @respx.mock
    async def test_ollama_server_both_endpoints(self):
        # modern ollama answers both; should be tagged ollama, use /v1
        respx.get("http://127.0.0.1:11434/v1/models").mock(
            return_value=httpx.Response(200, json=OPENAI_MODELS))
        respx.get("http://127.0.0.1:11434/api/tags").mock(
            return_value=httpx.Response(200, json=OLLAMA_TAGS))
        endpoint = await _probe("127.0.0.1", 11434)
        assert endpoint.kind == "ollama"
        assert endpoint.base_url == "http://127.0.0.1:11434/v1"
        assert endpoint.alias == "ollama_local_11434"

    @respx.mock
    async def test_old_ollama_tags_only(self):
        respx.get("http://127.0.0.1:11434/v1/models").mock(
            return_value=httpx.Response(404))
        respx.get("http://127.0.0.1:11434/api/tags").mock(
            return_value=httpx.Response(200, json=OLLAMA_TAGS))
        endpoint = await _probe("127.0.0.1", 11434)
        assert endpoint.kind == "ollama"
        assert endpoint.models == ("llama3.2:3b", "phi4")

    @respx.mock
    async def test_nothing_there(self):
        respx.get("http://127.0.0.1:9999/v1/models").mock(
            side_effect=httpx.ConnectError("refused"))
        respx.get("http://127.0.0.1:9999/api/tags").mock(
            side_effect=httpx.ConnectError("refused"))
        assert await _probe("127.0.0.1", 9999) is None

    @respx.mock
    async def test_web_server_that_is_not_llm(self):
        respx.get("http://127.0.0.1:8080/v1/models").mock(
            return_value=httpx.Response(200, json={"hello": "world"}))
        respx.get("http://127.0.0.1:8080/api/tags").mock(
            return_value=httpx.Response(200, json={"hello": "world"}))
        assert await _probe("127.0.0.1", 8080) is None


class TestScanAndCache:
    @respx.mock
    async def test_localhost_scan_finds_server(self):
        respx.get("http://127.0.0.1:8000/v1/models").mock(
            return_value=httpx.Response(200, json=OPENAI_MODELS))
        respx.route().mock(return_value=httpx.Response(404))
        endpoints = await scan(lan=False, timeout=10)
        assert len(endpoints) == 1
        assert endpoints[0].port == 8000

    def test_cache_roundtrip_and_ttl(self):
        fresh = Endpoint(base_url="http://x:1/v1", kind="openai", host="x", port=1,
                         models=("m",), checked_at=time.time())
        stale = Endpoint(base_url="http://y:2/v1", kind="openai", host="y", port=2,
                         models=("m",), checked_at=time.time() - 999_999)
        save_cache([fresh, stale])
        loaded = load_cache()
        assert [e.host for e in loaded] == ["x"]

    def test_corrupt_cache_ignored(self):
        from graphx.llm.discovery import cache_path
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        cache_path().write_text("{not json")
        assert load_cache() == []

    @respx.mock
    async def test_discover_revalidates_cache(self):
        save_cache([Endpoint(base_url="http://127.0.0.1:8000/v1", kind="openai",
                             host="127.0.0.1", port=8000, models=("old",),
                             checked_at=time.time())])
        respx.get("http://127.0.0.1:8000/v1/models").mock(
            return_value=httpx.Response(200, json=OPENAI_MODELS))
        respx.get("http://127.0.0.1:8000/api/tags").mock(
            return_value=httpx.Response(404))
        endpoints = await discover(lan=False)
        assert endpoints[0].models == ("qwen-big", "qwen-small")  # refreshed
        from graphx.llm.discovery import cache_path
        cached = json.loads(cache_path().read_text())
        assert cached[0]["models"] == ["qwen-big", "qwen-small"]  # cache rewritten


class TestBestEndpoint:
    def make(self, host, kind, models=("m",)):
        return Endpoint(base_url=f"http://{host}:1/v1", kind=kind, host=host,
                        port=1, models=models, checked_at=time.time())

    def test_prefers_openai_kind_then_localhost(self):
        lan_vllm = self.make("10.0.0.9", "openai")
        local_ollama = self.make("127.0.0.1", "ollama")
        assert best_endpoint([local_ollama, lan_vllm]) is lan_vllm

    def test_skips_model_less_endpoints(self):
        empty = self.make("127.0.0.1", "openai", models=())
        with_models = self.make("10.0.0.9", "ollama")
        assert best_endpoint([empty, with_models]) is with_models

    def test_empty(self):
        assert best_endpoint([]) is None
