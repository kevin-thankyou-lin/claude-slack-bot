from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog

from ..agent.backend import EventType, SessionEvent
from ..core.media import handle_custom_tool
from ..db import queries
from ..db.database import Database
from ..db.models import Thread
from ..slack.blocks import build_permission_block, build_summary_block
from ..slack.file_upload import scan_and_upload_files

logger = structlog.get_logger()

_CUSTOM_TOOLS = frozenset(("generate_image", "create_video", "post_summary"))


class ThreadCoordinator:
    """Maps Slack threads to agent sessions and orchestrates the conversation loop."""

    def __init__(self, backend: Any, db: Database) -> None:
        self.backend = backend
        self.db = db
        self._active: dict[str, asyncio.Task[None]] = {}

    async def handle_user_message(
        self,
        thread_ts: str,
        channel_id: str,
        text: str,
        say: Any,
        client: Any,
    ) -> None:
        """Route a user message to the appropriate agent session."""
        if thread_ts in self._active and not self._active[thread_ts].done():
            logger.warning("coordinator.thread_busy", thread_ts=thread_ts)
            return

        task = asyncio.create_task(self._process_message(thread_ts, channel_id, text, say, client))
        self._active[thread_ts] = task

    async def handle_tool_confirmation(
        self,
        tool_use_id: str,
        thread_ts: str,
        allowed: bool,
        say: Any,
        client: Any,
    ) -> None:
        """Handle a user's response to a tool confirmation prompt."""
        async with self.db._connect() as db:
            confirmation = await queries.get_pending_confirmation(db, tool_use_id)
            if confirmation is None:
                return

            thread = await queries.get_thread(db, thread_ts)
            if thread is None:
                return

            await queries.resolve_confirmation(db, tool_use_id, "allowed" if allowed else "denied")

        if not allowed:
            async for event in self.backend.send_tool_confirmation(thread.session_id, tool_use_id, allowed=False):
                await self._handle_event(event, thread_ts, thread.session_id, say, client)
            return

        tool_input = (
            json.loads(confirmation.tool_input)
            if isinstance(confirmation.tool_input, str)
            else confirmation.tool_input
        )
        result = await self._execute_tool(confirmation.tool_name, tool_input)
        async for event in self.backend.send_tool_result(thread.session_id, tool_use_id, result):
            await self._handle_event(event, thread_ts, thread.session_id, say, client)

    # ── internals ────────────────────────────────────────────────────────────

    async def _process_message(
        self,
        thread_ts: str,
        channel_id: str,
        text: str,
        say: Any,
        client: Any,
    ) -> None:
        try:
            async with self.db._connect() as db:
                thread = await queries.get_thread(db, thread_ts)

            if thread is None:
                session_id = await self.backend.create_session()
                thread = Thread(
                    thread_ts=thread_ts,
                    channel_id=channel_id,
                    session_id=session_id,
                    backend_type="messages",
                )
                async with self.db._connect() as db:
                    await queries.upsert_thread(db, thread)
                    await queries.add_message(db, thread_ts, "user", text)
                logger.info("coordinator.new_thread", thread_ts=thread_ts, session_id=session_id)
            else:
                async with self.db._connect() as db:
                    await queries.add_message(db, thread_ts, "user", text)

            # Sync auto-approve state to backend (relevant for Claude Code CLI)
            if thread.auto_approve and hasattr(self.backend, "set_auto_approve"):
                self.backend.set_auto_approve(thread.session_id, enabled=True)

            async for event in self.backend.send_message(thread.session_id, text):
                await self._handle_event(event, thread_ts, thread.session_id, say, client)

        except Exception:
            logger.exception("coordinator.process_error", thread_ts=thread_ts)
            await say(
                text=":warning: Something went wrong processing your message. Please try again.", thread_ts=thread_ts
            )

    async def _handle_event(
        self,
        event: SessionEvent,
        thread_ts: str,
        session_id: str,
        say: Any,
        client: Any,
    ) -> None:
        if event.type == EventType.TEXT:
            await self._handle_text(event, thread_ts, say)
        elif event.type == EventType.TOOL_CONFIRMATION_NEEDED:
            await self._handle_confirmation_needed(event, thread_ts, session_id, say, client)
        elif event.type == EventType.TOOL_USE:
            await self._handle_tool_use(event, thread_ts, session_id, say, client)
        elif event.type == EventType.ERROR:
            await say(text=f":warning: Error: {event.error_message}", thread_ts=thread_ts)
        elif event.type == EventType.TURN_END:
            logger.info("coordinator.turn_end", thread_ts=thread_ts)

    async def _handle_text(self, event: SessionEvent, thread_ts: str, say: Any) -> None:
        result = await say(text=event.text, thread_ts=thread_ts)
        slack_ts = result.get("ts") if isinstance(result, dict) else None
        async with self.db._connect() as db:
            await queries.add_message(db, thread_ts, "assistant", event.text, slack_msg_ts=slack_ts)

    async def _handle_confirmation_needed(
        self, event: SessionEvent, thread_ts: str, session_id: str, say: Any, client: Any
    ) -> None:
        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)

        if thread and thread.auto_approve:
            result = await self._execute_tool(event.tool_name, event.tool_input)
            async for follow_up in self.backend.send_tool_result(session_id, event.tool_use_id, result):
                await self._handle_event(follow_up, thread_ts, session_id, say, client)
        else:
            blocks = build_permission_block(event.tool_name, event.tool_input, event.tool_use_id)
            result = await say(text=f"Permission requested: `{event.tool_name}`", blocks=blocks, thread_ts=thread_ts)
            slack_ts = result.get("ts") if isinstance(result, dict) else None
            async with self.db._connect() as db:
                await queries.add_pending_confirmation(
                    db,
                    event.tool_use_id,
                    thread_ts,
                    event.tool_name,
                    event.tool_input,
                    slack_msg_ts=slack_ts,
                )

    async def _handle_tool_use(
        self, event: SessionEvent, thread_ts: str, session_id: str, say: Any, client: Any
    ) -> None:
        if event.tool_name in _CUSTOM_TOOLS:
            result = await self._handle_custom_tool(event, thread_ts, say, client)
        else:
            result = await self._execute_tool(event.tool_name, event.tool_input)
            channel_id = await self._get_channel_id(thread_ts)
            await scan_and_upload_files(
                client, channel_id, thread_ts, str(event.tool_input.get("command", "")), result
            )

        async for follow_up in self.backend.send_tool_result(session_id, event.tool_use_id, result):
            await self._handle_event(follow_up, thread_ts, session_id, say, client)

    async def _handle_custom_tool(self, event: SessionEvent, thread_ts: str, say: Any, client: Any) -> str:
        channel_id = await self._get_channel_id(thread_ts)
        if event.tool_name == "post_summary":
            summary = str(event.tool_input.get("summary", ""))
            status = str(event.tool_input.get("status", "completed"))
            blocks = build_summary_block(summary, status)
            await say(text=summary, blocks=blocks, thread_ts=thread_ts)
            return f"Summary posted: {summary}"
        return await handle_custom_tool(event.tool_name, event.tool_input, channel_id, thread_ts, client)

    async def _get_channel_id(self, thread_ts: str) -> str:
        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)
        return thread.channel_id if thread else ""

    async def _execute_tool(self, tool_name: str, tool_input: dict[str, object]) -> str:
        """Execute a tool and return its result as a string."""
        try:
            return await self._dispatch_tool(tool_name, tool_input)
        except asyncio.TimeoutError:
            return "Command timed out after 120 seconds"
        except Exception as e:
            return f"Tool execution error: {e}"

    async def _dispatch_tool(self, tool_name: str, tool_input: dict[str, object]) -> str:
        if tool_name == "bash":
            return await self._exec_bash(tool_input)
        if tool_name == "write_file":
            return self._exec_write_file(tool_input)
        if tool_name == "read_file":
            return self._exec_read_file(tool_input)
        return f"Unknown tool: {tool_name}"

    async def _exec_bash(self, tool_input: dict[str, object]) -> str:
        command = str(tool_input.get("command", ""))
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        output = stdout.decode(errors="replace")
        if stderr:
            output += "\nSTDERR:\n" + stderr.decode(errors="replace")
        if proc.returncode != 0:
            output += f"\n(exit code {proc.returncode})"
        return output[:50000]

    def _exec_write_file(self, tool_input: dict[str, object]) -> str:
        file_path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(content)
        return f"Wrote {len(content)} bytes to {file_path}"

    def _exec_read_file(self, tool_input: dict[str, object]) -> str:
        file_path = str(tool_input.get("path", ""))
        if not Path(file_path).exists():
            return f"File not found: {file_path}"
        return Path(file_path).read_text()[:50000]
