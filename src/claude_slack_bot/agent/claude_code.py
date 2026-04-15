from __future__ import annotations

import asyncio
import uuid
from typing import AsyncIterator

import structlog
from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)
from claude_code_sdk.types import PermissionResultAllow, StreamEvent, TextBlock, ToolPermissionContext

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

    One SDK client is created per unique working directory.  Each Slack thread
    maps to a ``session_id`` inside that client so multi-turn conversations
    cost only incremental tokens — no history replay.
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

        # One SDK client per cwd (keyed by resolved path, "" = default)
        self._clients: dict[str, ClaudeSDKClient] = {}
        self._lock = asyncio.Lock()

        # session_id -> cwd key (so we know which client to use)
        self._session_cwd: dict[str, str] = {}

    async def _get_client(self, cwd: str = "") -> ClaudeSDKClient:
        """Get or create an SDK client for the given working directory."""
        key = cwd or ""
        if key in self._clients:
            return self._clients[key]

        async with self._lock:
            if key in self._clients:
                return self._clients[key]

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
            self._clients[key] = client
            logger.info("claude_code_backend.client_connected", model=self.model, cwd=cwd or "(default)")
            return client

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        logger.info("claude_code_backend.session_created", session_id=session_id)
        return session_id

    def set_session_cwd(self, session_id: str, cwd: str) -> None:
        """Assign a working directory to a session (thread)."""
        self._session_cwd[session_id] = cwd

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        if enabled:
            self._auto_approve.add(session_id)
        else:
            self._auto_approve.discard(session_id)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a message via the appropriate SDK client for this session's cwd."""
        try:
            cwd = self._session_cwd.get(session_id, "")
            client = await self._get_client(cwd)
            got_deltas = False

            await client.query(content, session_id=session_id)

            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    delta_text = self._extract_text_delta(msg)
                    if delta_text:
                        got_deltas = True
                        yield SessionEvent(type=EventType.TEXT_DELTA, text=delta_text)
                elif isinstance(msg, AssistantMessage):
                    if not got_deltas:
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                yield SessionEvent(type=EventType.TEXT, text=block.text)
                    got_deltas = False
                elif isinstance(msg, ResultMessage):
                    yield SessionEvent(type=EventType.TURN_END, is_final=True)
                    return
                elif isinstance(msg, SystemMessage):
                    pass

            yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except Exception as e:
            logger.exception("claude_code_backend.send_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            # Reset the specific client that failed
            cwd = self._session_cwd.get(session_id, "")
            await self._reset_client(cwd)

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def shutdown(self) -> None:
        for cwd_key in list(self._clients.keys()):
            await self._reset_client(cwd_key)

    def _extract_text_delta(self, event: StreamEvent) -> str:
        evt = event.event
        if evt.get("type") == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return ""

    async def _reset_client(self, cwd_key: str = "") -> None:
        client = self._clients.pop(cwd_key, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.exception("claude_code_backend.disconnect_error", cwd=cwd_key)
