"""Golden-trace normalization: reduce an event stream to its behavioral
skeleton so two runs of the same graph diff cleanly (noise stripped)."""

from __future__ import annotations

import difflib

from ..engine.events import EventType, RunEvent

# event data keys that vary run-to-run and must not appear in a normalized trace
_VOLATILE = {"duration_s", "tokens", "cost_usd", "interrupt_id", "chunk", "run_id",
             "thread_id", "output", "state", "inputs", "delay_s", "input_hash"}
# chunk streaming is pure noise for behavioral comparison
_SKIP_TYPES = {EventType.NODE_OUTPUT_CHUNK, EventType.CHECKPOINT_SAVED}


def normalize(events: list[RunEvent]) -> list[str]:
    lines: list[str] = []
    for e in events:
        if e.type in _SKIP_TYPES:
            continue
        kept = {k: v for k, v in e.data.items() if k not in _VOLATILE}
        # state_updated.changed carries the actual values (volatile); keep only
        # WHICH channels changed — that is the behavioral signal, not the values.
        if isinstance(kept.get("changed"), dict):
            kept["changed"] = sorted(kept["changed"])
        node = f" {e.node_id}" if e.node_id else ""
        extra = f" {kept}" if kept else ""
        lines.append(f"s{e.step} {e.type.value}{node}{extra}")
    return lines


def diff(a: list[RunEvent], b: list[RunEvent]) -> list[str]:
    return [ln for ln in difflib.unified_diff(normalize(a), normalize(b),
                                              lineterm="", n=1)
            if not ln.startswith(("---", "+++", "@@"))]
