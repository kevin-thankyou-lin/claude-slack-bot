from __future__ import annotations

import pytest

from claude_slack_bot.core.permissions import PermissionManager
from claude_slack_bot.db import queries
from claude_slack_bot.db.database import Database
from claude_slack_bot.db.models import Thread


@pytest.mark.asyncio
async def test_auto_approve_default_off(db: Database) -> None:
    mgr = PermissionManager(db)
    assert await mgr.is_auto_approve("nonexistent") is False


@pytest.mark.asyncio
async def test_auto_approve_toggle(db: Database) -> None:
    mgr = PermissionManager(db)
    thread = Thread(
        thread_ts="1234567890.000010",
        channel_id="C123",
        session_id="sess-test",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)

    assert await mgr.is_auto_approve("1234567890.000010") is False
    await mgr.set_auto_approve("1234567890.000010", enabled=True)
    assert await mgr.is_auto_approve("1234567890.000010") is True
    await mgr.set_auto_approve("1234567890.000010", enabled=False)
    assert await mgr.is_auto_approve("1234567890.000010") is False
