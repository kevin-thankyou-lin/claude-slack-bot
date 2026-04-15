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
    """Agent backend using the Claude Code SDK with a persistent subprocess.

    Keeps one long-running ``claude`` process alive.  Each Slack thread maps
    to a ``session_id`` inside that process so multi-turn conversations cost
    only incremental tokens — no history replay.
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        max_turns: int = 30,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self._auto_approve: set[str] = set()
        self._active_session: str | None = None

        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> ClaudeSDKClient:
        """Lazily create and connect the SDK client."""
        if self._client is not None:
            return self._client

        async with self._lock:
            if self._client is not None:
                return self._client

            opts = ClaudeCodeOptions(
                model=self.model,
                max_turns=self.max_turns,
                append_system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                can_use_tool=_always_allow,
                include_partial_messages=True,
            )
            client = ClaudeSDKClient(opts)
            await client.connect()
            self._client = client
            logger.info("claude_code_backend.client_connected", model=self.model)
            return client

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        logger.info("claude_code_backend.session_created", session_id=session_id)
        return session_id

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        if enabled:
            self._auto_approve.add(session_id)
        else:
            self._auto_approve.discard(session_id)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a message via the persistent SDK client."""
        try:
            client = await self._ensure_client()
            self._active_session = session_id
            got_deltas = False

            await client.query(content, session_id=session_id)

            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    delta_text = self._extract_text_delta(msg)
                    if delta_text:
                        got_deltas = True
                        yield SessionEvent(type=EventType.TEXT_DELTA, text=delta_text)
                elif isinstance(msg, AssistantMessage):
                    # Skip full TEXT if we already streamed deltas — avoids duplicate posts
                    if not got_deltas:
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text:
                                yield SessionEvent(type=EventType.TEXT, text=block.text)
                    # Reset for next turn (agent may do multiple turns with tool use)
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
            await self._reset_client()

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def shutdown(self) -> None:
        await self._reset_client()

    def _extract_text_delta(self, event: StreamEvent) -> str:
        evt = event.event
        if evt.get("type") == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
        return ""

    async def _reset_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("claude_code_backend.disconnect_error")
            self._client = None
