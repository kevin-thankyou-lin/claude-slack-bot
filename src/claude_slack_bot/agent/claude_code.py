from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

import structlog
from claude_code_sdk import (
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_code_sdk.types import PermissionResultAllow, StreamEvent, ToolPermissionContext

from .backend import EventType, SessionEvent
from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()


async def _always_allow(
    tool_name: str, tool_input: dict[str, object], context: ToolPermissionContext
) -> PermissionResultAllow:
    """Permission callback that auto-approves every tool use."""
    return PermissionResultAllow()


class _ClientEntry:
    """A SDK client plus a lock to serialize concurrent access."""

    def __init__(self, client: ClaudeSDKClient) -> None:
        self.client = client
        self.lock = asyncio.Lock()


class ClaudeCodeBackend:
    """Agent backend using the Claude Code SDK with persistent subprocesses.

    One SDK client per unique working directory. Access to each client is
    serialized with a lock so concurrent threads don't interleave events.
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_turns: int = 30,
        cwd: str | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.default_cwd = cwd
        self._auto_approve: set[str] = set()

        self._entries: dict[str, _ClientEntry] = {}
        self._create_lock = asyncio.Lock()

        # session_id -> cwd key
        self._session_cwd: dict[str, str] = {}

    async def _get_entry(self, cwd: str = "") -> _ClientEntry:
        """Get or create a client entry for the given cwd."""
        key = cwd or ""
        if key in self._entries:
            return self._entries[key]

        async with self._create_lock:
            if key in self._entries:
                return self._entries[key]

            opts = ClaudeCodeOptions(
                model=self.model,
                max_turns=self.max_turns,
                append_system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                can_use_tool=_always_allow,
                include_partial_messages=True,
                cwd=cwd or self.default_cwd,
            )
            client = ClaudeSDKClient(opts)
            await client.connect()
            entry = _ClientEntry(client)
            self._entries[key] = entry
            logger.info("claude_code_backend.client_connected", model=self.model, cwd=cwd or "(default)")
            return entry

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        logger.info("claude_code_backend.session_created", session_id=session_id)
        return session_id

    def set_session_cwd(self, session_id: str, cwd: str) -> None:
        self._session_cwd[session_id] = cwd

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        if enabled:
            self._auto_approve.add(session_id)
        else:
            self._auto_approve.discard(session_id)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a message. Serialized per-client so concurrent threads don't interleave."""
        try:
            cwd = self._session_cwd.get(session_id, "")
            entry = await self._get_entry(cwd)

            # Serialize: only one query per client at a time
            async with entry.lock:
                await entry.client.query(content, session_id=session_id)

                async for msg in entry.client.receive_response():
                    if isinstance(msg, StreamEvent):
                        delta_text = self._extract_text_delta(msg)
                        if delta_text:
                            yield SessionEvent(type=EventType.TEXT_DELTA, text=delta_text)
                    elif isinstance(msg, ResultMessage):
                        yield SessionEvent(type=EventType.TURN_END, is_final=True)
                        return
                    # AssistantMessage/SystemMessage skipped — deltas cover text

                yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except Exception as e:
            logger.exception("claude_code_backend.send_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            cwd = self._session_cwd.get(session_id, "")
            await self._reset_entry(cwd)

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def shutdown(self) -> None:
        for key in list(self._entries.keys()):
            await self._reset_entry(key)

    def _extract_text_delta(self, event: StreamEvent) -> str:
        evt = event.event
        if evt.get("type") == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return ""

    async def _reset_entry(self, cwd_key: str = "") -> None:
        entry = self._entries.pop(cwd_key, None)
        if entry is not None:
            try:
                await entry.client.disconnect()
            except Exception:
                logger.exception("claude_code_backend.disconnect_error", cwd=cwd_key)
