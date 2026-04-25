from __future__ import annotations

import json

import aiosqlite

from .models import Message, PendingConfirmation, Poll, Thread

# ── Threads ────────────────────────────────────────────────────────────────────


async def get_thread(db: aiosqlite.Connection, thread_ts: str) -> Thread | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM threads WHERE thread_ts = ?", (thread_ts,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return Thread(
        thread_ts=row["thread_ts"],
        channel_id=row["channel_id"],
        session_id=row["session_id"],
        backend_type=row["backend_type"],
        auto_approve=bool(row["auto_approve"]),
        cwd=row["cwd"] if "cwd" in row.keys() else "",
        cc_session_id=row["cc_session_id"] if "cc_session_id" in row.keys() else "",
        model=row["model"] if "model" in row.keys() else "",
        effort=row["effort"] if "effort" in row.keys() else "",
        status=row["status"],
        user_id=row["user_id"] if "user_id" in row.keys() else "",
    )


async def upsert_thread(db: aiosqlite.Connection, thread: Thread) -> None:
    await db.execute(
        """INSERT INTO threads (thread_ts, channel_id, session_id, backend_type, auto_approve, cwd, cc_session_id, model, effort, status, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(thread_ts) DO UPDATE SET
               session_id = excluded.session_id,
               backend_type = excluded.backend_type,
               auto_approve = excluded.auto_approve,
               cwd = excluded.cwd,
               cc_session_id = excluded.cc_session_id,
               model = excluded.model,
               effort = excluded.effort,
               status = excluded.status,
               user_id = excluded.user_id,
               updated_at = datetime('now')""",
        (
            thread.thread_ts,
            thread.channel_id,
            thread.session_id,
            thread.backend_type,
            int(thread.auto_approve),
            thread.cwd,
            thread.cc_session_id,
            thread.model,
            thread.effort,
            thread.status,
            thread.user_id,
        ),
    )
    await db.commit()


async def set_cwd(db: aiosqlite.Connection, thread_ts: str, cwd: str) -> None:
    await db.execute(
        "UPDATE threads SET cwd = ?, updated_at = datetime('now') WHERE thread_ts = ?",
        (cwd, thread_ts),
    )
    await db.commit()


async def set_auto_approve(db: aiosqlite.Connection, thread_ts: str, *, enabled: bool) -> None:
    await db.execute(
        "UPDATE threads SET auto_approve = ?, updated_at = datetime('now') WHERE thread_ts = ?",
        (int(enabled), thread_ts),
    )
    await db.commit()


# ── Messages ───────────────────────────────────────────────────────────────────


async def add_message(
    db: aiosqlite.Connection,
    thread_ts: str,
    role: str,
    content: str,
    slack_msg_ts: str | None = None,
) -> int:
    async with db.execute(
        "INSERT INTO messages (thread_ts, role, content, slack_msg_ts) VALUES (?, ?, ?, ?)",
        (thread_ts, role, content, slack_msg_ts),
    ) as cur:
        row_id = cur.lastrowid
    await db.commit()
    return row_id or 0


async def get_messages(db: aiosqlite.Connection, thread_ts: str) -> list[Message]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM messages WHERE thread_ts = ? ORDER BY id ASC", (thread_ts,)) as cur:
        rows = await cur.fetchall()
    return [
        Message(
            id=row["id"],
            thread_ts=row["thread_ts"],
            role=row["role"],
            content=row["content"],
            slack_msg_ts=row["slack_msg_ts"],
        )
        for row in rows
    ]


async def get_message_count(db: aiosqlite.Connection, thread_ts: str) -> int:
    async with db.execute("SELECT COUNT(*) FROM messages WHERE thread_ts = ?", (thread_ts,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Polls ─────────────────────────────────────────────────────────────────────


async def upsert_poll(db: aiosqlite.Connection, poll: Poll) -> None:
    await db.execute(
        """INSERT INTO polls (thread_ts, channel_id, prompt, interval_secs, user_id)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(thread_ts) DO UPDATE SET
               channel_id = excluded.channel_id,
               prompt = excluded.prompt,
               interval_secs = excluded.interval_secs,
               user_id = excluded.user_id""",
        (poll.thread_ts, poll.channel_id, poll.prompt, poll.interval_secs, poll.user_id),
    )
    await db.commit()


async def delete_poll(db: aiosqlite.Connection, thread_ts: str) -> None:
    await db.execute("DELETE FROM polls WHERE thread_ts = ?", (thread_ts,))
    await db.commit()


async def get_all_polls(db: aiosqlite.Connection) -> list[Poll]:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM polls") as cur:
        rows = await cur.fetchall()
    return [
        Poll(
            thread_ts=row["thread_ts"],
            channel_id=row["channel_id"],
            prompt=row["prompt"],
            interval_secs=row["interval_secs"],
            user_id=row["user_id"],
        )
        for row in rows
    ]


# ── Pending confirmations ─────────────────────────────────────────────────────


async def add_pending_confirmation(
    db: aiosqlite.Connection,
    tool_use_id: str,
    thread_ts: str,
    tool_name: str,
    tool_input: dict[str, object],
    slack_msg_ts: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO pending_confirmations (tool_use_id, thread_ts, tool_name, tool_input, slack_msg_ts)
           VALUES (?, ?, ?, ?, ?)""",
        (tool_use_id, thread_ts, tool_name, json.dumps(tool_input), slack_msg_ts),
    )
    await db.commit()


async def get_pending_confirmation(db: aiosqlite.Connection, tool_use_id: str) -> PendingConfirmation | None:
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM pending_confirmations WHERE tool_use_id = ?", (tool_use_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return PendingConfirmation(
        tool_use_id=row["tool_use_id"],
        thread_ts=row["thread_ts"],
        tool_name=row["tool_name"],
        tool_input=row["tool_input"],
        status=row["status"],
        slack_msg_ts=row["slack_msg_ts"],
    )


async def resolve_confirmation(db: aiosqlite.Connection, tool_use_id: str, status: str) -> None:
    await db.execute(
        "UPDATE pending_confirmations SET status = ?, resolved_at = datetime('now') WHERE tool_use_id = ?",
        (status, tool_use_id),
    )
    await db.commit()


async def expire_old_confirmations(db: aiosqlite.Connection, timeout_seconds: int) -> list[PendingConfirmation]:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT * FROM pending_confirmations
           WHERE status = 'pending'
             AND created_at < datetime('now', ? || ' seconds')""",
        (f"-{timeout_seconds}",),
    ) as cur:
        rows = await cur.fetchall()

    expired = []
    for row in rows:
        expired.append(
            PendingConfirmation(
                tool_use_id=row["tool_use_id"],
                thread_ts=row["thread_ts"],
                tool_name=row["tool_name"],
                tool_input=row["tool_input"],
                status="expired",
                slack_msg_ts=row["slack_msg_ts"],
            )
        )
        await db.execute(
            "UPDATE pending_confirmations SET status = 'expired', resolved_at = datetime('now') WHERE tool_use_id = ?",
            (row["tool_use_id"],),
        )
    await db.commit()
    return expired
