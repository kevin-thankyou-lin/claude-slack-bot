from __future__ import annotations

import structlog

from ..db import queries
from ..db.database import Database

logger = structlog.get_logger()


class PermissionManager:
    """Manages per-thread auto-approve state and pending tool confirmations."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def is_auto_approve(self, thread_ts: str) -> bool:
        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)
        return thread.auto_approve if thread else False

    async def set_auto_approve(self, thread_ts: str, *, enabled: bool) -> None:
        async with self.db._connect() as db:
            await queries.set_auto_approve(db, thread_ts, enabled=enabled)
        logger.info("permissions.auto_approve_set", thread_ts=thread_ts, enabled=enabled)

    async def expire_stale(self, timeout_seconds: int) -> int:
        async with self.db._connect() as db:
            expired = await queries.expire_old_confirmations(db, timeout_seconds)
        if expired:
            logger.info("permissions.expired_confirmations", count=len(expired))
        return len(expired)
