"""SQLite storage: checkpoints, run events, node result cache."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    graph_name TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread ON checkpoints (thread_id, id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    node_id TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events (run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events (thread_id, id);

CREATE TABLE IF NOT EXISTS result_cache (
    node_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (node_id, input_hash)
);
"""

DEFAULT_DB_DIR = ".graphx"
DEFAULT_DB_NAME = "graphx.db"


def default_db_path(base: str | Path = ".") -> Path:
    return Path(base) / DEFAULT_DB_DIR / DEFAULT_DB_NAME


async def open_db(path: str | Path) -> aiosqlite.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.executescript(_SCHEMA)
    await db.commit()
    return db
