"""Discovery of local/LAN OpenAI-compatible inference servers.

Probes each host:port with GET /v1/models and /api/tags in parallel —
whichever answers identifies the server (OpenAI-compatible vs Ollama)
and the response doubles as the model list, so discovery is one round
trip. Recipe proven in the author's model-chat-cli project.

Results cache to ~/.graphx/providers.json (override dir with
GRAPHX_HOME); GRAPHX_NO_LAN_SCAN=1 restricts scanning to localhost;
GRAPHX_SCAN_PORTS overrides the probed ports.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_PORTS = (11434, 1234, 5000, 8000, 8080)   # ollama, lmstudio, misc, vllm, llama.cpp
PROBE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
SCAN_SEMAPHORE = 50
CACHE_TTL_S = 24 * 3600


def _ports() -> tuple[int, ...]:
    raw = os.environ.get("GRAPHX_SCAN_PORTS", "")
    if raw:
        return tuple(int(p) for p in raw.replace(",", " ").split())
    return DEFAULT_PORTS


def _home() -> Path:
    return Path(os.environ.get("GRAPHX_HOME", str(Path.home() / ".graphx")))


def cache_path() -> Path:
    return _home() / "providers.json"


@dataclass(frozen=True)
class Endpoint:
    base_url: str            # always .../v1 (modern Ollama serves /v1 too)
    kind: str                # "openai" | "ollama" — which probe answered
    host: str
    port: int
    models: tuple[str, ...] = ()
    response_ms: int = 0
    checked_at: float = 0.0

    @property
    def alias(self) -> str:
        prefix = "ollama" if self.kind == "ollama" else "openai"
        if self.host in ("127.0.0.1", "localhost", "::1"):
            return f"{prefix}_local_{self.port}"
        return f"{prefix}_{self.host.replace('.', '_')}_{self.port}"

    @property
    def label(self) -> str:
        models = f"{len(self.models)} model(s)" if self.models else "no models"
        return f"{self.alias}  {self.host}:{self.port}  [{self.kind}]  {models}"

    def provider_config(self) -> dict:
        return {"base_url": self.base_url, "protocol": "openai"}


# ------------------------------------------------------------------- probe

async def _get_json(client: httpx.AsyncClient, url: str):
    try:
        response = await client.get(url, timeout=PROBE_TIMEOUT)
        if response.status_code == 200:
            return response.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def probe(host: str, port: int, client: httpx.AsyncClient,
                semaphore: asyncio.Semaphore) -> Endpoint | None:
    async with semaphore:
        base = f"http://{host}:{port}"
        started = time.monotonic()
        openai_data, ollama_data = await asyncio.gather(
            _get_json(client, f"{base}/v1/models"),
            _get_json(client, f"{base}/api/tags"),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if openai_data is not None and isinstance(openai_data.get("data"), list):
            models = tuple(str(m.get("id")) for m in openai_data["data"] if m.get("id"))
            kind = "ollama" if ollama_data is not None else "openai"
            return Endpoint(base_url=f"{base}/v1", kind=kind, host=host, port=port,
                            models=models, response_ms=elapsed_ms,
                            checked_at=time.time())
        if ollama_data is not None and isinstance(ollama_data.get("models"), list):
            models = tuple(str(m.get("name")) for m in ollama_data["models"]
                           if m.get("name"))
            return Endpoint(base_url=f"{base}/v1", kind="ollama", host=host, port=port,
                            models=models, response_ms=elapsed_ms,
                            checked_at=time.time())
        return None


def _normalize_url(url: str) -> tuple[str, str, int]:
    """A typed value → (origin, host, port). Accepts bare host, host:port, or
    a full URL (https, trailing /v1, custom path stripped to origin)."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    host = parts.hostname or "localhost"
    port = parts.port or (443 if scheme == "https" else 80)
    netloc = f"{host}:{parts.port}" if parts.port else host
    return f"{scheme}://{netloc}", host, port


async def probe_url(url: str, client: httpx.AsyncClient | None = None) -> Endpoint | None:
    """Probe a user-typed endpoint URL directly (any scheme/host/port)."""
    origin, host, port = _normalize_url(url)
    own = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
    try:
        started = time.monotonic()
        openai_data, ollama_data = await asyncio.gather(
            _get_json(http, f"{origin}/v1/models"),
            _get_json(http, f"{origin}/api/tags"),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if openai_data is not None and isinstance(openai_data.get("data"), list):
            models = tuple(str(m.get("id")) for m in openai_data["data"] if m.get("id"))
            kind = "ollama" if ollama_data is not None else "openai"
            return Endpoint(base_url=f"{origin}/v1", kind=kind, host=host, port=port,
                            models=models, response_ms=elapsed_ms, checked_at=time.time())
        if ollama_data is not None and isinstance(ollama_data.get("models"), list):
            models = tuple(str(m.get("name")) for m in ollama_data["models"]
                           if m.get("name"))
            return Endpoint(base_url=f"{origin}/v1", kind="ollama", host=host, port=port,
                            models=models, response_ms=elapsed_ms, checked_at=time.time())
        return None
    finally:
        if own:
            await http.aclose()


async def add_endpoint(url: str) -> Endpoint | None:
    """Probe a URL and, if it's a live LLM server, merge it into the cache."""
    endpoint = await probe_url(url)
    if endpoint is None:
        return None
    others = [e for e in load_cache() if (e.host, e.port) != (endpoint.host, endpoint.port)]
    save_cache([endpoint, *others])
    return endpoint


def _local_subnet() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        return ip.rsplit(".", 1)[0]
    except OSError:
        return None


def lan_scan_enabled() -> bool:
    return os.environ.get("GRAPHX_NO_LAN_SCAN", "") not in ("1", "true", "yes")


async def scan(lan: bool = True, timeout: float = 30.0,
               progress: Callable[[str], None] | None = None) -> list[Endpoint]:
    """Probe localhost (always) and, optionally, this machine's /24."""
    ports = _ports()
    semaphore = asyncio.Semaphore(SCAN_SEMAPHORE)
    found: dict[tuple[str, int], Endpoint] = {}

    async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=25),
    ) as client:

        async def sweep(hosts: list[str]) -> None:
            tasks = [asyncio.create_task(probe(host, port, client, semaphore))
                     for host in hosts for port in ports]
            for task in asyncio.as_completed(tasks):
                endpoint = await task
                if endpoint is not None and (endpoint.host, endpoint.port) not in found:
                    found[(endpoint.host, endpoint.port)] = endpoint
                    if progress:
                        progress(endpoint.label)

        try:
            async with asyncio.timeout(timeout):
                await sweep(["127.0.0.1"])
                if lan and lan_scan_enabled():
                    subnet = _local_subnet()
                    if subnet:
                        local_ip = None
                        try:
                            local_ip = socket.gethostbyname(socket.gethostname())
                        except OSError:
                            pass
                        hosts = [f"{subnet}.{i}" for i in range(1, 256)
                                 if f"{subnet}.{i}" != local_ip]
                        await sweep(hosts)
        except TimeoutError:
            pass  # partial results are fine

    return sorted(found.values(), key=lambda e: (e.host != "127.0.0.1", e.response_ms))


# ------------------------------------------------------------------- cache

def save_cache(endpoints: list[Endpoint]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in endpoints], indent=2))


def load_cache(max_age_s: float = CACHE_TTL_S) -> list[Endpoint]:
    path = cache_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        endpoints = [Endpoint(**item) for item in raw]
    except (ValueError, TypeError):
        return []
    now = time.time()
    return [e for e in endpoints if now - e.checked_at <= max_age_s]


async def revalidate(endpoints: list[Endpoint]) -> list[Endpoint]:
    """Re-probe known hosts only (fast) — the mcc cache-validation flow."""
    semaphore = asyncio.Semaphore(SCAN_SEMAPHORE)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(probe(e.host, e.port, client, semaphore) for e in endpoints))
    return [e for e in results if e is not None]


async def discover(refresh: bool = False, lan: bool = True,
                   progress: Callable[[str], None] | None = None) -> list[Endpoint]:
    """cache → revalidate → full scan if empty (or refresh=True)."""
    if not refresh:
        cached = load_cache()
        if cached:
            live = await revalidate(cached)
            if live:
                save_cache(live)
                return live
    endpoints = await scan(lan=lan, progress=progress)
    if endpoints:
        save_cache(endpoints)
    return endpoints


def best_endpoint(endpoints: list[Endpoint]) -> Endpoint | None:
    """Prefer non-Ollama OpenAI-compat (vLLM etc. — usually the big model),
    then Ollama, localhost first within each group."""
    with_models = [e for e in endpoints if e.models]
    if not with_models:
        return endpoints[0] if endpoints else None
    return sorted(with_models,
                  key=lambda e: (e.kind == "ollama", e.host != "127.0.0.1"))[0]
