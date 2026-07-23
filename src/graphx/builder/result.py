"""BuildResult — the outcome of a build; never an exception to the caller."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..model.validate import Issue


@dataclass
class BuildResult:
    ok: bool
    yaml: str
    draft: Any = None                       # WorkflowDraft
    issues: list[Issue] = field(default_factory=list)
    exhausted: bool = False
    tokens: int = 0
    engine: str = "oneshot"
    model: str | None = None

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]
