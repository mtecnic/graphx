"""Human-in-the-loop: interrupt, checkpoint, resume."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class Interrupt:
    node_id: str
    payload: Any
    interrupt_id: str = field(default_factory=lambda: uuid4().hex[:12])


@dataclass(frozen=True)
class Command:
    """Input to Executor.run() to resume a paused/interrupted thread."""
    resume: Any = None
    update: Mapping[str, Any] | None = None
    goto: str | None = None
