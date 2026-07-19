"""LLMClient protocol handling via respx (no real providers)."""

import httpx
import pytest
import respx

from graphx.engine.errors import TransientError
from graphx.llm.client import LLMClient, LLMError, ToolDef


def client_for(base_url: str, protocol: str = "openai", pricing: dict | None = None):
    providers = {"test": {"base_url": base_url, "protocol": protocol,
                          **({"pricing": pricing} if pricing else {})}}
    return LLMClient(providers=providers)


class TestOpenAIProtocol:
    @respx.mock
    async def test_basic_chat_and_usage(self):
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            }))
        client = client_for("https://llm.test/v1",
                            pricing={"input_per_1m": 1.0, "output_per_1m": 2.0})
        response = await client.chat("test/m1", [{"role": "user", "content": "hey"}])
        assert response.text == "hi"
        assert response.usage.total == 15
        assert response.usage.cost_usd == pytest.approx((12 * 1 + 3 * 2) / 1e6)
        await client.aclose()

    @respx.mock
    async def test_tool_calls_parsed(self):
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={
                "choices": [{"message": {"content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "add", "arguments": '{"a": 1}'}}]},
                    "finish_reason": "tool_calls"}],
            }))
        client = client_for("https://llm.test/v1")
        response = await client.chat("test/m1", [{"role": "user", "content": "x"}],
                                     tools=[ToolDef(name="add")])
        assert response.tool_calls[0].name == "add"
        assert response.tool_calls[0].arguments == {"a": 1}
        await client.aclose()

    @respx.mock
    async def test_streaming_chunks(self):
        sse = ("data: " + '{"choices": [{"delta": {"content": "Hel"}}]}' + "\n\n"
               "data: " + '{"choices": [{"delta": {"content": "lo"}}]}' + "\n\n"
               "data: " + '{"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}' + "\n\n"
               "data: [DONE]\n\n")
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(200, content=sse.encode(),
                                        headers={"content-type": "text/event-stream"}))
        chunks: list[str] = []

        async def cb(chunk: str) -> None:
            chunks.append(chunk)

        client = client_for("https://llm.test/v1")
        response = await client.chat("test/m1", [{"role": "user", "content": "x"}],
                                     stream_cb=cb)
        assert response.text == "Hello"
        assert chunks == ["Hel", "lo"]
        assert response.usage.total == 7
        await client.aclose()

    @respx.mock
    async def test_429_maps_to_transient_with_retry_after(self):
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(429, text="rate limited",
                                        headers={"retry-after": "7"}))
        client = client_for("https://llm.test/v1")
        with pytest.raises(TransientError) as exc:
            await client.chat("test/m1", [{"role": "user", "content": "x"}])
        assert exc.value.retry_after == 7.0
        await client.aclose()

    @respx.mock
    async def test_401_is_deterministic(self):
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(401, text="bad key"))
        client = client_for("https://llm.test/v1")
        with pytest.raises(LLMError):
            await client.chat("test/m1", [{"role": "user", "content": "x"}])
        await client.aclose()

    async def test_unknown_provider(self):
        client = LLMClient()
        with pytest.raises(LLMError):
            await client.chat("nope/m", [])
        await client.aclose()


class TestAnthropicProtocol:
    @respx.mock
    async def test_messages_api_with_tools(self):
        route = respx.post("https://claude.test/v1/messages").mock(
            return_value=httpx.Response(200, json={
                "content": [{"type": "text", "text": "using tool"},
                            {"type": "tool_use", "id": "tu_1", "name": "add",
                             "input": {"a": 2}}],
                "usage": {"input_tokens": 9, "output_tokens": 4},
                "stop_reason": "tool_use",
            }))
        client = client_for("https://claude.test", protocol="anthropic")
        response = await client.chat(
            "test/claude-x", [{"role": "system", "content": "sys"},
                              {"role": "user", "content": "hi"}],
            tools=[ToolDef(name="add")])
        assert response.text == "using tool"
        assert response.tool_calls[0].arguments == {"a": 2}
        payload = route.calls[0].request.read()
        import json
        body = json.loads(payload)
        assert body["system"] == "sys"                      # system extracted
        assert body["messages"][0]["role"] == "user"
        assert body["tools"][0]["input_schema"] is not None
        await client.aclose()
