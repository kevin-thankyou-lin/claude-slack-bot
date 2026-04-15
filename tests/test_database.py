from __future__ import annotations

import pytest

from claude_slack_bot.db import queries
from claude_slack_bot.db.database import Database
from claude_slack_bot.db.models import Thread


@pytest.mark.asyncio
async def test_thread_crud(db: Database) -> None:
    thread = Thread(
        thread_ts="1234567890.000001",
        channel_id="C123",
        session_id="sess-abc",
        backend_type="messages",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)
        result = await queries.get_thread(conn, "1234567890.000001")

    assert result is not None
    assert result.session_id == "sess-abc"
    assert result.auto_approve is False


@pytest.mark.asyncio
async def test_auto_approve(db: Database) -> None:
    thread = Thread(
        thread_ts="1234567890.000002",
        channel_id="C123",
        session_id="sess-def",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)
        await queries.set_auto_approve(conn, "1234567890.000002", enabled=True)
        result = await queries.get_thread(conn, "1234567890.000002")

    assert result is not None
    assert result.auto_approve is True


@pytest.mark.asyncio
async def test_messages(db: Database) -> None:
    thread = Thread(
        thread_ts="1234567890.000003",
        channel_id="C123",
        session_id="sess-ghi",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)
        await queries.add_message(conn, "1234567890.000003", "user", "hello")
        await queries.add_message(conn, "1234567890.000003", "assistant", "hi there")
        messages = await queries.get_messages(conn, "1234567890.000003")

    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].content == "hi there"


@pytest.mark.asyncio
async def test_pending_confirmations(db: Database) -> None:
    thread = Thread(
        thread_ts="1234567890.000004",
        channel_id="C123",
        session_id="sess-jkl",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)
        await queries.add_pending_confirmation(
            conn,
            tool_use_id="tool-123",
            thread_ts="1234567890.000004",
            tool_name="bash",
            tool_input={"command": "ls"},
        )
        pc = await queries.get_pending_confirmation(conn, "tool-123")

    assert pc is not None
    assert pc.tool_name == "bash"
    assert pc.status == "pending"

    async with db._connect() as conn:
        await queries.resolve_confirmation(conn, "tool-123", "allowed")
        pc = await queries.get_pending_confirmation(conn, "tool-123")

    assert pc is not None
    assert pc.status == "allowed"


@pytest.mark.asyncio
async def test_message_count(db: Database) -> None:
    thread = Thread(
        thread_ts="1234567890.000005",
        channel_id="C123",
        session_id="sess-mno",
    )
    async with db._connect() as conn:
        await queries.upsert_thread(conn, thread)
        assert await queries.get_message_count(conn, "1234567890.000005") == 0
        await queries.add_message(conn, "1234567890.000005", "user", "test")
        assert await queries.get_message_count(conn, "1234567890.000005") == 1
