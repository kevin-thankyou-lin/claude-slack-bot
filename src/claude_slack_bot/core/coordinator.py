from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
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


STREAM_FLUSH_INTERVAL = 3.0  # seconds between Slack message updates
STREAM_FIRST_POST_DELAY = 0.5  # wait this long before first post (avoids flicker for fast replies)


class _StreamBuffer:
    """Accumulates text deltas and posts a single Slack message.

    Posts the first message after STREAM_FIRST_POST_DELAY, then updates
    it in-place every STREAM_FLUSH_INTERVAL. On finalize, removes the
    typing indicator. If the response finishes before the first post
    delay, posts just once with no indicator.
    """

    def __init__(self, thread_ts: str, say: Any, client: Any, user_id: str = "") -> None:
        self.thread_ts = thread_ts
        self._say = say
        self._client = client
        self._user_id = user_id
        self._text = ""
        self._slack_msg_ts: str | None = None
        self._channel_id: str | None = None
        self._dirty = False
        self._first_delta_time: float | None = None
        # Set by coordinator to track the "Thinking..." message
        self._thinking_ts: str | None = None
        self._thinking_channel: str | None = None

    async def append(self, delta: str) -> None:
        self._text += delta
        self._dirty = True
        if self._first_delta_time is None:
            self._first_delta_time = time.monotonic()

    async def flush(self) -> None:
        if not self._dirty or not self._text:
            return

        # Don't post yet if we're still within the first-post delay
        if self._slack_msg_ts is None and self._first_delta_time is not None:
            elapsed = time.monotonic() - self._first_delta_time
            if elapsed < STREAM_FIRST_POST_DELAY:
                return

        self._dirty = False

        if self._slack_msg_ts is None:
            # Replace the "Thinking..." message if we have one
            if self._thinking_ts and self._thinking_channel:
                try:
                    await self._client.chat_update(
                        channel=self._thinking_channel,
                        ts=self._thinking_ts,
                        text=self._text + " :writing_hand:",
                    )
                    self._slack_msg_ts = self._thinking_ts
                    self._channel_id = self._thinking_channel
                    self._thinking_ts = None
                    self._thinking_channel = None
                except Exception:
                    logger.warning("stream_buffer.thinking_update_failed", thread_ts=self.thread_ts)
            else:
                result = await self._say(text=self._text + " :writing_hand:", thread_ts=self.thread_ts)
                if isinstance(result, dict):
                    self._slack_msg_ts = result.get("ts")
                    self._channel_id = result.get("channel")
        elif self._channel_id:
            try:
                await self._client.chat_update(
                    channel=self._channel_id,
                    ts=self._slack_msg_ts,
                    text=self._text + " :writing_hand:",
                )
            except Exception:
                logger.warning("stream_buffer.update_failed", thread_ts=self.thread_ts)

    async def finalize(self) -> str:
        """Post final text — no typing indicator. Mentions user to signal task complete."""
        self._dirty = False
        mention = f"<@{self._user_id}> " if self._user_id else ""
        final_text = mention + self._text if mention and self._text else self._text

        # Determine which message to update (streamed message or thinking message)
        msg_ts = self._slack_msg_ts or self._thinking_ts
        channel = self._channel_id or self._thinking_channel

        if msg_ts and channel and final_text:
            try:
                await self._client.chat_update(channel=channel, ts=msg_ts, text=final_text)
            except Exception:
                logger.warning("stream_buffer.finalize_failed", thread_ts=self.thread_ts)
        elif self._text:
            # Never posted yet (fast response) — post once cleanly
            await self._say(text=final_text, thread_ts=self.thread_ts)
        return self._text

    @property
    def has_content(self) -> bool:
        return bool(self._text)


class ThreadCoordinator:
    """Maps Slack threads to agent sessions and orchestrates the conversation loop."""

    def __init__(self, backend: Any, db: Database, projects_dir: str = "/home/linke/Projects") -> None:
        self.backend = backend
        self.db = db
        self.projects_dir = Path(projects_dir)
        self._active: dict[str, asyncio.Task[None]] = {}
        self._stream_buffers: dict[str, _StreamBuffer] = {}
        self._polls: dict[str, asyncio.Task[None]] = {}  # thread_ts -> poll task

    async def handle_user_message(
        self,
        thread_ts: str,
        channel_id: str,
        text: str,
        say: Any,
        client: Any,
        user_id: str = "",
    ) -> None:
        """Route a user message to the appropriate agent session."""
        # Handle `cd <path>` — optionally followed by a message on the same line
        # e.g. "cd gr00t" or "cd gr00t check the eval results"
        cd_match = re.match(r"^cd\s+(\S+)\s*(.*)?$", text.strip(), re.DOTALL)
        if cd_match:
            cd_path = cd_match.group(1).strip()
            remaining = (cd_match.group(2) or "").strip()
            await self._handle_cd(thread_ts, channel_id, cd_path, say, user_id=user_id)
            if remaining:
                # Process the rest as a normal message
                if thread_ts in self._active and not self._active[thread_ts].done():
                    logger.warning("coordinator.thread_busy", thread_ts=thread_ts)
                    await say(
                        text=":hourglass: Still working on the previous request... please wait.", thread_ts=thread_ts
                    )
                else:
                    task = asyncio.create_task(
                        self._process_message(thread_ts, channel_id, remaining, say, client, user_id=user_id)
                    )
                    self._active[thread_ts] = task
            return

        # Handle poll command: "poll 10m check status" or "poll stop"
        poll_match = re.match(r"^poll\s+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        if poll_match:
            await self._handle_poll(thread_ts, channel_id, poll_match.group(1).strip(), say, client, user_id)
            return

        # Handle stop/cancel command (also stops polls)
        if text.strip().lower() in ("stop", "cancel", "abort", "nevermind", "nvm"):
            await self._handle_stop(thread_ts, say)
            return

        # Handle done — stop task and disconnect the client to free resources
        if text.strip().lower() == "done":
            await self._handle_done(thread_ts, say)
            return

        # Handle btw (side-channel question — runs in parallel, doesn't block the main task)
        btw_match = re.match(r"^btw[:\s]+(.+)$", text.strip(), re.IGNORECASE | re.DOTALL)
        if btw_match:
            btw_text = btw_match.group(1).strip()
            _btw_task = asyncio.create_task(self._process_btw(thread_ts, channel_id, btw_text, say, client, user_id))  # noqa: RUF006
            return

        if thread_ts in self._active and not self._active[thread_ts].done():
            logger.warning("coordinator.thread_busy", thread_ts=thread_ts)
            await say(text=":hourglass: Still working on the previous request... please wait.", thread_ts=thread_ts)
            return

        task = asyncio.create_task(self._process_message(thread_ts, channel_id, text, say, client, user_id=user_id))
        self._active[thread_ts] = task

    def _resolve_cwd(self, path: str) -> Path | None:
        """Resolve a path or folder name to an absolute directory."""
        # Try as absolute path first
        candidate = Path(path).expanduser().resolve()
        if candidate.is_dir():
            return candidate

        # Try as a folder name under projects_dir
        candidate = (self.projects_dir / path).resolve()
        if candidate.is_dir():
            return candidate

        # Try case-insensitive match under projects_dir
        if self.projects_dir.is_dir():
            for child in self.projects_dir.iterdir():
                if child.is_dir() and child.name.lower() == path.lower():
                    return child

        return None

    async def _handle_cd(self, thread_ts: str, channel_id: str, path: str, say: Any, user_id: str = "") -> None:
        """Set the working directory for a thread."""
        resolved = self._resolve_cwd(path)
        if resolved is None:
            await say(
                text=f":x: Directory not found: `{path}`\nTry a full path or folder name under `{self.projects_dir}`",
                thread_ts=thread_ts,
            )
            return

        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)
            if thread is None:
                session_id = await self.backend.create_session()
                thread = Thread(
                    thread_ts=thread_ts,
                    channel_id=channel_id,
                    session_id=session_id,
                    backend_type="claude-code",
                    cwd=str(resolved),
                    user_id=user_id,
                )
                await queries.upsert_thread(db, thread)
            else:
                await queries.set_cwd(db, thread_ts, str(resolved))

        # Tell the backend which cwd this session should use
        if hasattr(self.backend, "set_session_cwd"):
            await self.backend.set_session_cwd(thread.session_id, str(resolved))

        await say(text=f":file_folder: Working directory set to `{resolved}`", thread_ts=thread_ts)
        logger.info("coordinator.cwd_set", thread_ts=thread_ts, cwd=str(resolved))

    async def _handle_stop(self, thread_ts: str, say: Any) -> None:
        """Cancel the running task and any poll for a thread."""
        # Cancel poll if active
        poll_task = self._polls.pop(thread_ts, None)
        if poll_task:
            poll_task.cancel()

        task = self._active.get(thread_ts)
        if task and not task.done():
            # Interrupt the Claude process
            async with self.db._connect() as db:
                thread = await queries.get_thread(db, thread_ts)
            if thread and hasattr(self.backend, "interrupt"):
                await self.backend.interrupt(thread.session_id)

            task.cancel()
            self._active.pop(thread_ts, None)
            self._stream_buffers.pop(thread_ts, None)
            await say(text=":octagonal_sign: Stopped. Send a new message to continue.", thread_ts=thread_ts)
            logger.info("coordinator.stopped", thread_ts=thread_ts)
        else:
            await say(text="Nothing running in this thread.", thread_ts=thread_ts)

    async def _handle_done(self, thread_ts: str, say: Any) -> None:
        """Mark thread as done — stop any running task, poll, and disconnect the client."""
        # Cancel poll if active
        poll_task = self._polls.pop(thread_ts, None)
        if poll_task:
            poll_task.cancel()

        # Stop running task if any
        task = self._active.get(thread_ts)
        if task and not task.done():
            async with self.db._connect() as db:
                thread = await queries.get_thread(db, thread_ts)
            if thread and hasattr(self.backend, "interrupt"):
                await self.backend.interrupt(thread.session_id)
            task.cancel()
            self._active.pop(thread_ts, None)
            self._stream_buffers.pop(thread_ts, None)

        # Disconnect the client to free resources
        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)
        if thread and hasattr(self.backend, "_reset_client"):
            await self.backend._reset_client(thread.session_id)

        await say(
            text=":white_check_mark: Done. Thread closed. Start a new message for a fresh conversation.",
            thread_ts=thread_ts,
        )
        logger.info("coordinator.done", thread_ts=thread_ts)

    async def _handle_poll(
        self, thread_ts: str, channel_id: str, args: str, say: Any, client: Any, user_id: str
    ) -> None:
        """Start or stop a recurring poll in this thread."""
        if args.strip().lower() == "stop":
            poll_task = self._polls.pop(thread_ts, None)
            if poll_task:
                poll_task.cancel()
                await say(text=":octagonal_sign: Poll stopped.", thread_ts=thread_ts)
            else:
                await say(text="No active poll in this thread.", thread_ts=thread_ts)
            return

        # Parse: "poll 10m check osmo status" or "poll 1h check status"
        interval_match = re.match(r"^(\d+)\s*(m|min|h|hr|s|sec)\s+(.+)$", args.strip(), re.IGNORECASE | re.DOTALL)
        if not interval_match:
            await say(
                text="Usage: `poll <interval> <prompt>`\nExamples: `poll 10m check osmo status`, `poll 1h check eval results`, `poll stop`",
                thread_ts=thread_ts,
            )
            return

        amount = int(interval_match.group(1))
        unit = interval_match.group(2).lower()
        prompt = interval_match.group(3).strip()

        if unit in ("m", "min"):
            interval_secs = amount * 60
        elif unit in ("h", "hr"):
            interval_secs = amount * 3600
        else:
            interval_secs = amount

        # Cancel existing poll if any
        old_poll = self._polls.pop(thread_ts, None)
        if old_poll:
            old_poll.cancel()

        # Start the poll
        poll_task = asyncio.create_task(
            self._run_poll(thread_ts, channel_id, prompt, interval_secs, say, client, user_id)
        )
        self._polls[thread_ts] = poll_task

        unit_label = f"{amount}{'m' if unit in ('m', 'min') else unit[0]}"
        await say(
            text=f":repeat: Poll started — will run `{prompt}` every {unit_label}. Type `poll stop` to cancel.",
            thread_ts=thread_ts,
        )
        logger.info("coordinator.poll_started", thread_ts=thread_ts, interval=interval_secs, prompt=prompt)

    async def _run_poll(
        self,
        thread_ts: str,
        channel_id: str,
        prompt: str,
        interval_secs: int,
        say: Any,
        client: Any,
        user_id: str,
    ) -> None:
        """Run a prompt on a recurring interval, letting Claude decide what to do each tick."""
        poll_prompt = (
            f"{prompt}\n\n"
            "---\n"
            "_This is an automated periodic check. Based on the results:_\n"
            "- _If still in progress and looks healthy, give a brief status update._\n"
            "- _If something needs fixing, go ahead and fix it._\n"
            "- _If the task is complete or no longer needs monitoring, "
            "include POLL_COMPLETE in your response to stop this recurring check._\n"
        )
        try:
            while True:
                await asyncio.sleep(interval_secs)
                # Wait for any active task to finish first
                active = self._active.get(thread_ts)
                if active and not active.done():
                    logger.info("coordinator.poll_skipped_busy", thread_ts=thread_ts)
                    continue

                logger.info("coordinator.poll_tick", thread_ts=thread_ts, prompt=prompt)
                task = asyncio.create_task(
                    self._process_message(thread_ts, channel_id, poll_prompt, say, client, user_id=user_id)
                )
                self._active[thread_ts] = task

                # Wait for this tick to finish so we can check the response
                await task

                # Check if Claude signalled completion via the stream buffer's final text
                # The finalized text was stored in the DB — read the latest assistant message
                async with self.db._connect() as db:
                    messages = await queries.get_messages(db, thread_ts)
                if messages:
                    last_msg = messages[-1]
                    if last_msg.role == "assistant" and "POLL_COMPLETE" in last_msg.content:
                        logger.info("coordinator.poll_auto_stopped", thread_ts=thread_ts)
                        self._polls.pop(thread_ts, None)
                        await say(text=":white_check_mark: Poll auto-stopped (task complete).", thread_ts=thread_ts)
                        return

        except asyncio.CancelledError:
            logger.info("coordinator.poll_cancelled", thread_ts=thread_ts)

    async def _process_btw(
        self,
        thread_ts: str,
        channel_id: str,
        text: str,
        say: Any,
        client: Any,
        user_id: str = "",
    ) -> None:
        """Process a side-channel 'btw' question on a temporary session."""
        btw_session = f"btw-{uuid.uuid4().hex}"
        try:
            # Copy the thread's cwd to the temp session
            async with self.db._connect() as db:
                thread = await queries.get_thread(db, thread_ts)
            if thread and thread.cwd and hasattr(self.backend, "set_session_cwd"):
                await self.backend.set_session_cwd(btw_session, thread.cwd)

            # Post thinking indicator
            thinking_result = await say(text=":speech_balloon: btw — thinking...", thread_ts=thread_ts)
            thinking_ts = thinking_result.get("ts") if isinstance(thinking_result, dict) else None
            thinking_channel = thinking_result.get("channel") if isinstance(thinking_result, dict) else None

            buf_key = f"btw:{thread_ts}:{btw_session}"
            buf = _StreamBuffer(thread_ts, say, client, user_id=user_id)
            buf._thinking_ts = thinking_ts
            buf._thinking_channel = thinking_channel
            self._stream_buffers[buf_key] = buf

            async def _periodic_flush() -> None:
                while True:
                    await asyncio.sleep(STREAM_FLUSH_INTERVAL)
                    await buf.flush()

            flush_task = asyncio.create_task(_periodic_flush())
            try:
                async for event in self.backend.send_message(btw_session, text):
                    await self._handle_event(event, buf_key, btw_session, user_id, say, client)
            finally:
                flush_task.cancel()
                if buf.has_content:
                    await buf.finalize()
                self._stream_buffers.pop(buf_key, None)

            # Clean up the temporary session
            if hasattr(self.backend, "_reset_client"):
                await self.backend._reset_client(btw_session)

        except Exception:
            logger.exception("coordinator.btw_error", thread_ts=thread_ts)
            await say(text=":warning: btw question failed.", thread_ts=thread_ts)

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

        user_id = thread.user_id

        if not allowed:
            async for event in self.backend.send_tool_confirmation(thread.session_id, tool_use_id, allowed=False):
                await self._handle_event(event, thread_ts, thread.session_id, user_id, say, client)
            return

        tool_input = (
            json.loads(confirmation.tool_input)
            if isinstance(confirmation.tool_input, str)
            else confirmation.tool_input
        )
        result = await self._execute_tool(confirmation.tool_name, tool_input)
        async for event in self.backend.send_tool_result(thread.session_id, tool_use_id, result):
            await self._handle_event(event, thread_ts, thread.session_id, user_id, say, client)

    # ── internals ────────────────────────────────────────────────────────────

    async def _process_message(
        self,
        thread_ts: str,
        channel_id: str,
        text: str,
        say: Any,
        client: Any,
        user_id: str = "",
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
                    user_id=user_id,
                )
                async with self.db._connect() as db:
                    await queries.upsert_thread(db, thread)
                    await queries.add_message(db, thread_ts, "user", text)
                logger.info("coordinator.new_thread", thread_ts=thread_ts, session_id=session_id)
            else:
                # Backfill user_id if it wasn't stored on the original thread
                if not thread.user_id and user_id:
                    thread.user_id = user_id
                    async with self.db._connect() as db:
                        await queries.upsert_thread(db, thread)
                async with self.db._connect() as db:
                    await queries.add_message(db, thread_ts, "user", text)

            # Resolve effective user_id (prefer thread record, fall back to event)
            effective_user_id = thread.user_id or user_id

            # Sync per-thread state to backend
            if thread.auto_approve and hasattr(self.backend, "set_auto_approve"):
                self.backend.set_auto_approve(thread.session_id, enabled=True)
            if thread.cwd and hasattr(self.backend, "set_session_cwd"):
                await self.backend.set_session_cwd(thread.session_id, thread.cwd)
            # Restore Claude Code session ID for resume after restart
            if thread.cc_session_id and hasattr(self.backend, "set_cc_session_id"):
                self.backend.set_cc_session_id(thread.session_id, thread.cc_session_id)

            message = text

            # Post a "thinking" indicator immediately so user sees activity
            thinking_result = await say(text=":brain: Thinking...", thread_ts=thread_ts)
            thinking_ts = thinking_result.get("ts") if isinstance(thinking_result, dict) else None
            thinking_channel = thinking_result.get("channel") if isinstance(thinking_result, dict) else None

            # Create a stream buffer for live updates
            buf = _StreamBuffer(thread_ts, say, client, user_id=effective_user_id)
            buf._thinking_ts = thinking_ts  # track so we can delete it later
            buf._thinking_channel = thinking_channel
            self._stream_buffers[thread_ts] = buf

            async def _periodic_flush() -> None:
                while True:
                    await asyncio.sleep(STREAM_FLUSH_INTERVAL)
                    await buf.flush()

            flush_task = asyncio.create_task(_periodic_flush())
            try:
                async for event in self.backend.send_message(thread.session_id, message):
                    await self._handle_event(event, thread_ts, thread.session_id, effective_user_id, say, client)
            finally:
                flush_task.cancel()
                if buf.has_content:
                    final_text = await buf.finalize()
                    async with self.db._connect() as db_conn:
                        await queries.add_message(db_conn, thread_ts, "assistant", final_text)
                self._stream_buffers.pop(thread_ts, None)

                # Persist the Claude Code session ID for resume after restart
                if hasattr(self.backend, "get_cc_session_id"):
                    cc_sid = self.backend.get_cc_session_id(thread.session_id)
                    if cc_sid and cc_sid != thread.cc_session_id:
                        thread.cc_session_id = cc_sid
                        async with self.db._connect() as db_conn:
                            await queries.upsert_thread(db_conn, thread)

        except Exception:
            logger.exception("coordinator.process_error", thread_ts=thread_ts)
            mention = f"<@{user_id}> " if user_id else ""
            await say(
                text=f"{mention}:warning: Something went wrong processing your message. Please try again.",
                thread_ts=thread_ts,
            )

    async def _handle_event(
        self,
        event: SessionEvent,
        thread_ts: str,
        session_id: str,
        user_id: str,
        say: Any,
        client: Any,
    ) -> None:
        if event.type == EventType.TEXT_DELTA:
            buf = self._stream_buffers.get(thread_ts)
            if buf:
                await buf.append(event.text)
            # If no buffer (non-streaming backend), fall through to TEXT handling
        elif event.type == EventType.TEXT:
            # If we have a stream buffer, the deltas already covered this text
            buf = self._stream_buffers.get(thread_ts)
            if not buf or not buf.has_content:
                await self._handle_text(event, thread_ts, user_id, say)
        elif event.type == EventType.TOOL_CONFIRMATION_NEEDED:
            await self._handle_confirmation_needed(event, thread_ts, session_id, user_id, say, client)
        elif event.type == EventType.TOOL_USE:
            await self._handle_tool_use(event, thread_ts, session_id, user_id, say, client)
        elif event.type == EventType.ERROR:
            mention = f"<@{user_id}> " if user_id else ""
            await say(text=f"{mention}:warning: Error: {event.error_message}", thread_ts=thread_ts)
        elif event.type == EventType.TURN_END:
            logger.info("coordinator.turn_end", thread_ts=thread_ts)

    async def _handle_text(self, event: SessionEvent, thread_ts: str, user_id: str, say: Any) -> None:
        mention = f"<@{user_id}> " if user_id else ""
        text = mention + event.text if mention else event.text
        result = await say(text=text, thread_ts=thread_ts)
        slack_ts = result.get("ts") if isinstance(result, dict) else None
        async with self.db._connect() as db:
            await queries.add_message(db, thread_ts, "assistant", event.text, slack_msg_ts=slack_ts)

    async def _handle_confirmation_needed(
        self, event: SessionEvent, thread_ts: str, session_id: str, user_id: str, say: Any, client: Any
    ) -> None:
        async with self.db._connect() as db:
            thread = await queries.get_thread(db, thread_ts)

        if thread and thread.auto_approve:
            result = await self._execute_tool(event.tool_name, event.tool_input)
            async for follow_up in self.backend.send_tool_result(session_id, event.tool_use_id, result):
                await self._handle_event(follow_up, thread_ts, session_id, user_id, say, client)
        else:
            mention = f"<@{user_id}> " if user_id else ""
            blocks = build_permission_block(event.tool_name, event.tool_input, event.tool_use_id)
            result = await say(
                text=f"{mention}Permission requested: `{event.tool_name}`",
                blocks=blocks,
                thread_ts=thread_ts,
            )
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
        self, event: SessionEvent, thread_ts: str, session_id: str, user_id: str, say: Any, client: Any
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
            await self._handle_event(follow_up, thread_ts, session_id, user_id, say, client)

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
