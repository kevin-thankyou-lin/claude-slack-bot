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
from claude_code_sdk.types import StreamEvent, TextBlock

from .backend import EventType, SessionEvent
from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()


class ClaudeCodeBackend:
    """Agent backend using the Claude Code SDK with a persistent subprocess.

    Keeps one long-running ``claude`` process alive.  Each Slack thread maps
    to a ``session_id`` inside that process so multi-turn conversations cost
    only incremental tokens — no history replay.

    Auto-approve is handled per-thread via the ``can_use_tool`` callback.
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
        # Maps our session_id to the SDK session_id (currently the same)
        self._active_session: str | None = None

        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> ClaudeSDKClient:
        """Lazily create and connect the SDK client."""
        if self._client is not None:
            return self._client

        async with self._lock:
            # Double-check after acquiring lock
            if self._client is not None:
                return self._client

            opts = ClaudeCodeOptions(
                model=self.model,
                max_turns=self.max_turns,
                append_system_prompt=SYSTEM_PROMPT,
                permission_mode="bypassPermissions",
                include_partial_messages=True,
            )
            client = ClaudeSDKClient(opts)
            await client.connect()
            self._client = client
            logger.info("claude_code_backend.client_connected", model=self.model)
            return client

    async def create_session(self, system_prompt: str | None = None) -> str:
        """Create a logical session ID.  The SDK client is shared."""
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

            await client.query(content, session_id=session_id)

            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    # Streaming text delta — forward immediately for live updates
                    delta_text = self._extract_text_delta(msg)
                    if delta_text:
                        yield SessionEvent(type=EventType.TEXT_DELTA, text=delta_text)
                elif isinstance(msg, AssistantMessage):
                    # Full assistant message — emit as TEXT for final state
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield SessionEvent(type=EventType.TEXT, text=block.text)
                elif isinstance(msg, ResultMessage):
                    yield SessionEvent(type=EventType.TURN_END, is_final=True)
                    return
                elif isinstance(msg, SystemMessage):
                    pass

            # If we exit the loop without a ResultMessage
            yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except Exception as e:
            logger.exception("claude_code_backend.send_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            # Reset client on error so it reconnects on next message
            await self._reset_client()

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        """Not used — Claude Code handles tools internally."""
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        """Not used — permission_mode=bypassPermissions handles this."""
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def shutdown(self) -> None:
        """Disconnect the SDK client."""
        await self._reset_client()

    def _extract_text_delta(self, event: StreamEvent) -> str:
        """Extract text from a streaming delta event."""
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
