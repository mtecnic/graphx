"""Injectable services: everything a node touches that tests need to fake."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests: sleep() advances time instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # yield to the loop


@dataclass
class Services:
    clock: Clock = field(default_factory=RealClock)
    rng: random.Random = field(default_factory=random.Random)
    llm: Any = None       # LLMClient (v0.2)
    http: Any = None      # httpx.AsyncClient (v0.2)
    mcp: Any = None       # McpManager (v0.2)
