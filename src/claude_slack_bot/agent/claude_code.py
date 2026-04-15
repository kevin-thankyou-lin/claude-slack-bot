from __future__ import annotations

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


class ClaudeCodeBackend:
    """Agent backend using the Claude Code SDK with persistent subprocesses.

    One SDK client per Slack thread. Each thread gets its own ``claude``
    process so conversations run fully in parallel with no cross-talk.
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

        # One SDK client per session (thread)
        self._clients: dict[str, ClaudeSDKClient] = {}
        # session_id -> cwd
        self._session_cwd: dict[str, str] = {}

    async def _get_client(self, session_id: str) -> ClaudeSDKClient:
        """Get or create an SDK client for this session."""
        if session_id in self._clients:
            return self._clients[session_id]

        cwd = self._session_cwd.get(session_id) or self.default_cwd
        opts = ClaudeCodeOptions(
            model=self.model,
            max_turns=self.max_turns,
            append_system_prompt=SYSTEM_PROMPT,
            permission_mode="bypassPermissions",
            can_use_tool=_always_allow,
            include_partial_messages=True,
            cwd=cwd,
        )
        client = ClaudeSDKClient(opts)
        await client.connect()
        self._clients[session_id] = client
        logger.info("claude_code_backend.client_connected", session_id=session_id, cwd=cwd or "(default)")
        return client

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        logger.info("claude_code_backend.session_created", session_id=session_id)
        return session_id

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        self._session_cwd[session_id] = cwd
        # If client already exists for this session, disconnect it
        # so it reconnects with the new cwd on next message
        if session_id in self._clients:
            await self._reset_client(session_id)

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        if enabled:
            self._auto_approve.add(session_id)
        else:
            self._auto_approve.discard(session_id)

    def interrupt(self, session_id: str) -> None:
        """Interrupt the running query for a session."""
        client = self._clients.get(session_id)
        if client:
            client.interrupt()
            logger.info("claude_code_backend.interrupted", session_id=session_id)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a message. Each session has its own client — fully parallel, no cross-talk."""
        try:
            client = await self._get_client(session_id)

            await client.query(content, session_id=session_id)

            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    delta_text = self._extract_text_delta(msg)
                    if delta_text:
                        yield SessionEvent(type=EventType.TEXT_DELTA, text=delta_text)
                elif isinstance(msg, ResultMessage):
                    yield SessionEvent(type=EventType.TURN_END, is_final=True)
                    return

            yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except Exception as e:
            logger.exception("claude_code_backend.send_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            await self._reset_client(session_id)

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def shutdown(self) -> None:
        for sid in list(self._clients.keys()):
            await self._reset_client(sid)

    def _extract_text_delta(self, event: StreamEvent) -> str:
        evt = event.event
        if evt.get("type") == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return ""

    async def _reset_client(self, session_id: str) -> None:
        client = self._clients.pop(session_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("claude_code_backend.disconnect_error", session_id=session_id)
