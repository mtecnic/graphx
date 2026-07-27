"""Eval dataset: cases with inputs + expectations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from ruamel.yaml import YAML


class JudgeSpec(BaseModel):
    artifact: str                     # <state.x>/<node.field> ref
    criteria: str
    model: str
    min_score: float = 0.7


class Expect(BaseModel):
    status: str | None = "finished"
    no_dead_letters: bool = True
    assert_: list[str] = Field(default_factory=list, alias="assert")
    budget: dict[str, float] = Field(default_factory=dict)   # {tokens, cost_usd}
    judge: JudgeSpec | None = None

    model_config = {"populate_by_name": True}


class EvalCase(BaseModel):
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    expect: Expect = Field(default_factory=Expect)


class EvalDataset(BaseModel):
    version: int = 1
    dataset: str
    cases: list[EvalCase]


def load_dataset(path: str | Path) -> EvalDataset:
    with open(path) as f:
        data = YAML(typ="safe", pure=True).load(f)
    return EvalDataset.model_validate(data)
