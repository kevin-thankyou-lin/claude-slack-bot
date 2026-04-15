from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_ts       TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    backend_type    TEXT NOT NULL DEFAULT 'messages',
    auto_approve    INTEGER NOT NULL DEFAULT 0,
    cwd             TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    user_id         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_ts       TEXT NOT NULL REFERENCES threads(thread_ts),
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    slack_msg_ts    TEXT
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    tool_use_id     TEXT PRIMARY KEY,
    thread_ts       TEXT NOT NULL REFERENCES threads(thread_ts),
    tool_name       TEXT NOT NULL,
    tool_input      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    slack_msg_ts    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_ts);
CREATE INDEX IF NOT EXISTS idx_confirmations_thread ON pending_confirmations(thread_ts);
CREATE INDEX IF NOT EXISTS idx_confirmations_status ON pending_confirmations(status);
CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        os.makedirs(Path(self.db_path).parent, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            # Migrate: add user_id column to existing databases
            try:
                await db.execute("ALTER TABLE threads ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
                await db.commit()
            except Exception:
                pass  # Column already exists

    def _connect(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path)
