"""Read-only reader over the events + checkpoints tables.

Deliberately does not subclass or mutate SqliteCheckpointer — it opens
its own queries so the ops layer stays a pure observer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..engine.checkpoint import RunMetrics
from ..engine.events import RunEvent

_TERMINAL = {"run_finished": "finished", "run_failed": "failed",
             "run_interrupted": "interrupted", "run_cancelled": "cancelled"}


@dataclass(frozen=True)
class RunRow:
    run_id: str
    thread_id: str
    workflow: str
    status: str
    steps: int
    started: str
    ended: str


class EventStore:
    """Wraps an open aiosqlite connection with read-only run queries."""

    def __init__(self, db):
        self.db = db

    async def list_runs(self, limit: int = 50, workflow: str | None = None) -> list[RunRow]:
        async with self.db.execute(
            "SELECT run_id, thread_id, "
            "  MIN(ts) AS started, MAX(ts) AS ended, MAX(step) AS steps, "
            "  MAX(CASE WHEN type='run_started' "
            "      THEN json_extract(data,'$.graph') END) AS workflow, "
            "  (SELECT type FROM events e2 WHERE e2.run_id = events.run_id "
            "   ORDER BY e2.id DESC LIMIT 1) AS last_type "
            "FROM events GROUP BY run_id ORDER BY MIN(id) DESC LIMIT ?",
            (max(1, limit) if workflow is None else 1000,),
        ) as cursor:
            rows = await cursor.fetchall()
        out = []
        for r in rows:
            wf = r["workflow"] or "?"
            if workflow is not None and wf != workflow:
                continue
            out.append(RunRow(
                run_id=r["run_id"], thread_id=r["thread_id"], workflow=wf,
                status=_TERMINAL.get(r["last_type"] or "", "running"),
                steps=r["steps"] or 0, started=r["started"] or "", ended=r["ended"] or ""))
        return out[:limit] if workflow is not None else out

    async def run_events(self, run_or_thread_id: str) -> list[RunEvent]:
        async with self.db.execute(
            "SELECT run_id, thread_id, seq, step, ts, type, node_id, data FROM events "
            "WHERE run_id = ? OR thread_id = ? ORDER BY id",
            (run_or_thread_id, run_or_thread_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [RunEvent(seq=r["seq"], ts=r["ts"], run_id=r["run_id"],
                         thread_id=r["thread_id"], step=r["step"], type=r["type"],
                         node_id=r["node_id"], data=json.loads(r["data"])) for r in rows]

    async def run_metrics(self, run_or_thread_id: str) -> RunMetrics | None:
        """Cumulative RunMetrics from the latest checkpoint of that thread/run."""
        async with self.db.execute(
            "SELECT data FROM checkpoints WHERE thread_id = ? OR run_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (run_or_thread_id, run_or_thread_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        data: dict[str, Any] = json.loads(row["data"])
        return RunMetrics(**data["metrics"])
