from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Protocol


class EventType(Enum):
    TEXT = "text"
    TEXT_DELTA = "text_delta"
    TOOL_ACTIVITY = "tool_activity"
    TOOL_USE = "tool_use"
    TOOL_CONFIRMATION_NEEDED = "tool_confirmation_needed"
    TOOL_RESULT = "tool_result"
    TURN_END = "turn_end"
    ERROR = "error"


@dataclass
class SessionEvent:
    type: EventType
    text: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, object] = field(default_factory=dict)
    error_message: str = ""
    is_final: bool = False


class AgentBackend(Protocol):
    async def create_session(self, system_prompt: str) -> str:
        """Create a new agent session, return a session identifier."""
        ...

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a user message and yield response events."""
        ...

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        """Send a tool result back to the session and yield follow-up events."""
        ...

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        """Send a tool confirmation (allow/deny) and yield follow-up events."""
        ...
