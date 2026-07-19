"""SQLite implementation of the Checkpointer protocol + event sink."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..engine.checkpoint import Checkpoint, CheckpointMeta
from ..engine.events import RunEvent


def _dumps(value: Any) -> str:
    return json.dumps(value, default=repr, separators=(",", ":"))


class SqliteCheckpointer:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def save(self, ckpt: Checkpoint) -> None:
        await self.db.execute(
            "INSERT INTO checkpoints (thread_id, run_id, step, graph_name, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (ckpt.thread_id, ckpt.run_id, ckpt.step, ckpt.graph_name,
             _dumps(ckpt.to_json())),
        )
        await self.db.commit()

    async def load_latest(self, thread_id: str) -> Checkpoint | None:
        async with self.db.execute(
            "SELECT data FROM checkpoints WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
            (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return Checkpoint.from_json(json.loads(row["data"]))

    async def history(self, thread_id: str) -> list[CheckpointMeta]:
        async with self.db.execute(
            "SELECT thread_id, run_id, step, saved_at FROM checkpoints "
            "WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [CheckpointMeta(row["thread_id"], row["run_id"], row["step"],
                               row["saved_at"]) for row in rows]

    async def cache_get(self, node_id: str, input_hash: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT output FROM result_cache WHERE node_id = ? AND input_hash = ?",
            (node_id, input_hash),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["output"]) if row else None

    async def cache_put(self, node_id: str, input_hash: str, output: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO result_cache (node_id, input_hash, output) "
            "VALUES (?, ?, ?)",
            (node_id, input_hash, _dumps(output)),
        )
        await self.db.commit()

    # ------------------------------------------------------------- events

    async def event_sink(self, event: RunEvent) -> None:
        await self.db.execute(
            "INSERT INTO events (run_id, thread_id, seq, step, ts, type, node_id, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.run_id, event.thread_id, event.seq, event.step, event.ts.isoformat(),
             event.type.value, event.node_id, _dumps(event.data)),
        )
        await self.db.commit()

    async def events_for(self, run_or_thread_id: str) -> list[RunEvent]:
        async with self.db.execute(
            "SELECT run_id, thread_id, seq, step, ts, type, node_id, data FROM events "
            "WHERE run_id = ? OR thread_id = ? ORDER BY id",
            (run_or_thread_id, run_or_thread_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [RunEvent(
            seq=row["seq"], ts=row["ts"], run_id=row["run_id"],
            thread_id=row["thread_id"], step=row["step"], type=row["type"],
            node_id=row["node_id"], data=json.loads(row["data"]),
        ) for row in rows]
