"""Thin multi-provider LLM client. No SDK dependencies — just httpx.

Model strings are ``provider/model`` (``ollama/qwen3:8b``,
``openai/gpt-4o-mini``, ``anthropic/claude-sonnet-5``). Providers are
resolved from the workflow's ``providers:`` section merged over
built-in defaults; anything OpenAI-compatible (vLLM, Ollama, LM Studio,
gateways) works via ``protocol: openai``.

Transport errors, 429s and 5xx map to TransientError (with Retry-After
when the server sends it) so the engine's retry/fallback layers do
their job; 4xx are deterministic LLMErrors.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..engine.errors import GraphxError, TransientError

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY",
               "protocol": "openai"},
    "anthropic": {"base_url": "https://api.anthropic.com", "api_key_env": "ANTHROPIC_API_KEY",
                  "protocol": "anthropic"},
    "ollama": {"base_url": "http://localhost:11434/v1", "protocol": "openai"},
    "vllm": {"base_url": "http://localhost:8000/v1", "protocol": "openai"},
    "llamacpp": {"base_url": "http://localhost:8080/v1", "protocol": "openai"},
    "lmstudio": {"base_url": "http://localhost:1234/v1", "protocol": "openai"},
}

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(GraphxError):
    """Deterministic LLM failure (auth, bad request, unknown model)."""


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=lambda: {"type": "object",
                                                                   "properties": {}})


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    stop_reason: str | None = None


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class LLMClient:
    def __init__(self, providers: Mapping[str, Any] | None = None,
                 http: httpx.AsyncClient | None = None, timeout_s: float = 120.0,
                 resolver: Any = None):
        merged = {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()}
        for name, cfg in (providers or {}).items():
            merged.setdefault(name, {}).update(cfg or {})
        self.providers = merged
        self._resolver = resolver          # SecretResolver, or None
        self._own_http = http is None
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def aclose(self) -> None:
        if self._own_http:
            await self.http.aclose()

    def _resolve(self, model: str) -> tuple[dict[str, Any], str]:
        provider_name, sep, model_name = model.partition("/")
        if not sep:
            raise LLMError(f"model must be 'provider/model', got {model!r}")
        provider = self.providers.get(provider_name)
        if provider is None:
            raise LLMError(f"unknown provider '{provider_name}' "
                           f"(have: {', '.join(sorted(self.providers))})")
        return provider, model_name

    def _headers(self, provider: dict[str, Any]) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key_env = provider.get("api_key_env")
        key = os.environ.get(key_env, "") if key_env else ""
        # a provider may instead set `api_key: secret://NAME` (or a literal)
        raw_key = provider.get("api_key")
        if raw_key and self._resolver is not None:
            key = self._resolver.resolve(raw_key, strict=False) or key
        elif raw_key:
            key = str(raw_key)
        if provider.get("protocol") == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        elif key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response, body: str) -> None:
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = None
            raw = response.headers.get("retry-after")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            raise TransientError(f"LLM HTTP {response.status_code}: {body[:300]}",
                                 retry_after=retry_after)
        if response.status_code >= 400:
            raise LLMError(f"LLM HTTP {response.status_code}: {body[:500]}")

    def _cost(self, provider: dict[str, Any], usage: Usage) -> float:
        pricing = provider.get("pricing") or {}
        return (usage.prompt_tokens * float(pricing.get("input_per_1m", 0)) +
                usage.completion_tokens * float(pricing.get("output_per_1m", 0))) / 1_000_000

    # ------------------------------------------------------------------ chat

    async def chat(self, model: str, messages: list[dict[str, Any]],
                   tools: list[ToolDef] | None = None,
                   stream_cb: Callable[[str], Awaitable[None]] | None = None,
                   temperature: float | None = None,
                   max_tokens: int | None = None,
                   force_json: bool = False) -> LLMResponse:
        provider, model_name = self._resolve(model)
        protocol = provider.get("protocol", "openai")
        try:
            if protocol == "anthropic":
                return await self._chat_anthropic(provider, model_name, messages, tools,
                                                  stream_cb, temperature, max_tokens)
            return await self._chat_openai(provider, model_name, messages, tools,
                                           stream_cb, temperature, max_tokens, force_json)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"LLM connection failed: {exc}") from exc

    # ---------------------------------------------------- openai-compatible

    async def _chat_openai(self, provider, model_name, messages, tools, stream_cb,
                           temperature, max_tokens, force_json) -> LLMResponse:
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {"model": model_name, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": dict(t.parameters)}} for t in tools]
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        stream = stream_cb is not None and not tools
        if not stream:
            response = await self.http.post(url, json=payload,
                                            headers=self._headers(provider))
            self._raise_for_status(response, response.text)
            return self._parse_openai(provider, model_name, response.json())

        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        usage = Usage()
        async with self.http.stream("POST", url, json=payload,
                                    headers=self._headers(provider)) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                self._raise_for_status(response, body)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage.prompt_tokens = chunk["usage"].get("prompt_tokens", 0)
                    usage.completion_tokens = chunk["usage"].get("completion_tokens", 0)
                for choice in chunk.get("choices", []):
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        text_parts.append(delta)
                        await stream_cb(delta)
        text = "".join(text_parts)
        if usage.total == 0:
            usage.prompt_tokens = _estimate_tokens(json.dumps(messages))
            usage.completion_tokens = _estimate_tokens(text)
        usage.cost_usd = self._cost(provider, usage)
        return LLMResponse(text=text, tool_calls=[], usage=usage, model=model_name)

    def _parse_openai(self, provider, model_name, data: dict[str, Any]) -> LLMResponse:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError) as exc:
            raise LLMError(f"malformed LLM response: {json.dumps(data)[:300]}") from exc
        tool_calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": fn.get("arguments")}
            tool_calls.append(ToolCall(id=call.get("id", ""), name=fn.get("name", ""),
                                       arguments=arguments))
        raw_usage = data.get("usage") or {}
        usage = Usage(prompt_tokens=raw_usage.get("prompt_tokens", 0),
                      completion_tokens=raw_usage.get("completion_tokens", 0))
        usage.cost_usd = self._cost(provider, usage)
        return LLMResponse(text=message.get("content") or "", tool_calls=tool_calls,
                           usage=usage, model=model_name,
                           stop_reason=choice.get("finish_reason"))

    # ------------------------------------------------------------ anthropic

    async def _chat_anthropic(self, provider, model_name, messages, tools, stream_cb,
                              temperature, max_tokens) -> LLMResponse:
        url = provider["base_url"].rstrip("/") + "/v1/messages"
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                # Anthropic requires ALL tool_results for one assistant turn in a
                # single user turn — coalesce consecutive tool messages.
                block = {"type": "tool_result",
                         "tool_use_id": message.get("tool_call_id", ""),
                         "content": str(message.get("content", ""))}
                if converted and converted[-1]["role"] == "user" \
                        and isinstance(converted[-1]["content"], list) \
                        and converted[-1]["content"][0].get("type") == "tool_result":
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            elif role == "assistant" and message.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message["tool_calls"]:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({"type": "tool_use", "id": call.get("id", ""),
                                   "name": fn.get("name", ""), "input": args})
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": role, "content": message.get("content", "")})

        payload: dict[str, Any] = {"model": model_name, "messages": converted,
                                   "max_tokens": max_tokens or 4096}
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [{"name": t.name, "description": t.description,
                                 "input_schema": dict(t.parameters)} for t in tools]

        stream = stream_cb is not None and not tools
        if not stream:
            response = await self.http.post(url, json=payload,
                                            headers=self._headers(provider))
            self._raise_for_status(response, response.text)
            return self._parse_anthropic(provider, model_name, response.json())

        payload["stream"] = True
        text_parts: list[str] = []
        usage = Usage()
        async with self.http.stream("POST", url, json=payload,
                                    headers=self._headers(provider)) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                self._raise_for_status(response, body)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta["text"])
                        await stream_cb(delta["text"])
                elif etype == "message_start":
                    usage.prompt_tokens = ((event.get("message") or {}).get("usage")
                                           or {}).get("input_tokens", 0)
                elif etype == "message_delta":
                    usage.completion_tokens = (event.get("usage")
                                               or {}).get("output_tokens", 0)
        text = "".join(text_parts)
        if usage.total == 0:
            usage.prompt_tokens = _estimate_tokens(json.dumps(messages))
            usage.completion_tokens = _estimate_tokens(text)
        usage.cost_usd = self._cost(provider, usage)
        return LLMResponse(text=text, tool_calls=[], usage=usage, model=model_name)

    def _parse_anthropic(self, provider, model_name, data: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(id=block.get("id", ""),
                                           name=block.get("name", ""),
                                           arguments=block.get("input") or {}))
        raw_usage = data.get("usage") or {}
        usage = Usage(prompt_tokens=raw_usage.get("input_tokens", 0),
                      completion_tokens=raw_usage.get("output_tokens", 0))
        usage.cost_usd = self._cost(provider, usage)
        return LLMResponse(text="".join(text_parts), tool_calls=tool_calls, usage=usage,
                           model=model_name, stop_reason=data.get("stop_reason"))
