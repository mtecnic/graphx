"""Workflow triggers — how a run starts by itself.

A `triggers:` list in the workflow YAML declares when/why the workflow
runs. The engine ignores the section (build_graph reads only known
keys); the daemon (server) and `graphx schedule` read it here.

    triggers:
      - { type: schedule, cron: "0 7 * * *", input: { topic: news } }
      - { type: interval, every: 15m }
      - { type: webhook,  path: "orders", input_from: body }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_FACTORS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, None: 1.0}


class TriggerError(ValueError):
    pass


def _duration(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise TriggerError(f"bad duration {value!r} (use e.g. 30s, 15m, 1h, 1d)")
    return float(match.group(1)) * _FACTORS[match.group(2)]


@dataclass(frozen=True)
class Trigger:
    type: str                       # schedule | interval | webhook
    cron: str | None = None
    every_s: float | None = None
    path: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    input_from: str | None = None   # "body" → webhook request body becomes the input

    def describe(self) -> str:
        if self.type == "schedule":
            return f"schedule cron='{self.cron}'"
        if self.type == "interval":
            return f"interval every {self.every_s:g}s"
        return f"webhook POST /hooks/{self.path}"


def parse_trigger(raw: dict[str, Any]) -> Trigger:
    ttype = raw.get("type")
    if ttype == "schedule":
        cron = raw.get("cron")
        if not cron:
            raise TriggerError("schedule trigger needs a 'cron' expression")
        _validate_cron(cron)
        return Trigger("schedule", cron=str(cron), input=dict(raw.get("input") or {}))
    if ttype == "interval":
        every = raw.get("every")
        if every is None:
            raise TriggerError("interval trigger needs 'every' (e.g. 15m)")
        every_s = _duration(every)
        if every_s <= 0:
            raise TriggerError(f"interval 'every' must be positive, got {every!r}")
        return Trigger("interval", every_s=every_s, input=dict(raw.get("input") or {}))
    if ttype == "webhook":
        path = raw.get("path")
        if not path:
            raise TriggerError("webhook trigger needs a 'path'")
        return Trigger("webhook", path=str(path).strip("/"),
                       input=dict(raw.get("input") or {}),
                       input_from=raw.get("input_from", "body"))
    raise TriggerError(f"unknown trigger type {ttype!r} "
                       "(use schedule | interval | webhook)")


def _validate_cron(expr: str) -> None:
    try:
        from croniter import croniter
    except ImportError:
        return  # croniter absent (no [server] extra) — validated at daemon start
    if not croniter.is_valid(str(expr)):
        raise TriggerError(f"invalid cron expression {expr!r}")


def load_triggers(source: str | Path | dict[str, Any]) -> list[Trigger]:
    if isinstance(source, dict):
        data = source
    else:
        from .model.yaml_loader import load_raw
        data = load_raw(source)
    raw_triggers = data.get("triggers") or []
    if not isinstance(raw_triggers, list):
        raise TriggerError("'triggers' must be a list")
    # parse each independently: one malformed trigger must not drop the others
    out: list[Trigger] = []
    for raw in raw_triggers:
        try:
            out.append(parse_trigger(dict(raw)))
        except TriggerError as exc:
            import logging
            logging.getLogger("graphx.triggers").warning("skipping trigger: %s", exc)
    return out
