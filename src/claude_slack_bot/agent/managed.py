from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

import anthropic
import structlog

from .backend import EventType, SessionEvent

logger = structlog.get_logger()


class ManagedAgentBackend:
    """Agent backend using the Anthropic Managed Agents API (beta).

    Each Slack thread maps to one Anthropic session. Sessions are stateful
    and managed server-side, so we do not need to replay conversation history.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        agent_id: str,
        agent_version: int = 1,
    ) -> None:
        self.client = client
        self.agent_id = agent_id
        self.agent_version = agent_version

    async def create_session(self, system_prompt: str | None = None) -> str:
        """Create a new managed agent session."""
        try:
            # Create an environment for this session
            environment = await self.client.beta.environments.create(  # type: ignore[attr-defined]
                name=f"slack-{uuid.uuid4().hex[:8]}",
                config={"type": "cloud", "networking": {"type": "unrestricted"}},
            )

            session = await self.client.beta.sessions.create(  # type: ignore[attr-defined]
                agent={"type": "agent", "id": self.agent_id, "version": self.agent_version},
                environment_id=environment.id,
            )

            logger.info(
                "managed_backend.session_created",
                session_id=session.id,
                environment_id=environment.id,
            )
            return session.id

        except anthropic.APIError as e:
            logger.error("managed_backend.create_failed", error=str(e))
            raise

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a user message to the managed session and stream events."""
        try:
            async with self.client.beta.sessions.events.stream(session_id) as stream:  # type: ignore[attr-defined]
                await self.client.beta.sessions.events.send(  # type: ignore[attr-defined]
                    session_id,
                    events=[
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": content}],
                        }
                    ],
                )

                async for event in stream:
                    for normalized in self._normalize_event(event):
                        yield normalized
                        if normalized.type == EventType.TURN_END:
                            return

        except anthropic.APIError as e:
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        """Send a tool confirmation and stream follow-up events."""
        try:
            result = "allow" if allowed else "deny"
            deny_message = None if allowed else "User denied this action."

            event_payload: dict[str, Any] = {
                "type": "user.tool_confirmation",
                "tool_use_id": tool_use_id,
                "result": result,
            }
            if deny_message:
                event_payload["deny_message"] = deny_message

            async with self.client.beta.sessions.events.stream(session_id) as stream:  # type: ignore[attr-defined]
                await self.client.beta.sessions.events.send(  # type: ignore[attr-defined]
                    session_id,
                    events=[event_payload],
                )

                async for event in stream:
                    for normalized in self._normalize_event(event):
                        yield normalized
                        if normalized.type == EventType.TURN_END:
                            return

        except anthropic.APIError as e:
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        """Send a custom tool result back to the session."""
        try:
            async with self.client.beta.sessions.events.stream(session_id) as stream:  # type: ignore[attr-defined]
                await self.client.beta.sessions.events.send(  # type: ignore[attr-defined]
                    session_id,
                    events=[
                        {
                            "type": "user.custom_tool_result",
                            "tool_use_id": tool_use_id,
                            "content": [{"type": "text", "text": result}],
                        }
                    ],
                )

                async for event in stream:
                    for normalized in self._normalize_event(event):
                        yield normalized
                        if normalized.type == EventType.TURN_END:
                            return

        except anthropic.APIError as e:
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))

    def _normalize_event(self, event: Any) -> list[SessionEvent]:
        """Convert Anthropic session events to our normalized SessionEvent."""
        results: list[SessionEvent] = []
        event_type = getattr(event, "type", "")

        if event_type == "agent.message":
            for block in getattr(event, "content", []):
                if getattr(block, "type", "") == "text":
                    results.append(SessionEvent(type=EventType.TEXT, text=block.text))

        elif event_type == "agent.tool_use":
            permission = getattr(event, "evaluated_permission", "allow")
            if permission == "ask":
                results.append(
                    SessionEvent(
                        type=EventType.TOOL_CONFIRMATION_NEEDED,
                        tool_use_id=event.id,
                        tool_name=getattr(event, "name", ""),
                        tool_input=getattr(event, "input", {}),
                    )
                )
            else:
                results.append(
                    SessionEvent(
                        type=EventType.TOOL_USE,
                        tool_use_id=event.id,
                        tool_name=getattr(event, "name", ""),
                        tool_input=getattr(event, "input", {}),
                    )
                )

        elif event_type == "agent.custom_tool_use":
            results.append(
                SessionEvent(
                    type=EventType.TOOL_USE,
                    tool_use_id=event.id,
                    tool_name=getattr(event, "name", ""),
                    tool_input=getattr(event, "input", {}),
                )
            )

        elif event_type == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            if stop_reason and getattr(stop_reason, "type", "") == "end_turn":
                results.append(SessionEvent(type=EventType.TURN_END, is_final=True))

        elif event_type == "session.error":
            error_msg = getattr(event, "error", {})
            results.append(
                SessionEvent(
                    type=EventType.ERROR,
                    error_message=str(error_msg),
                )
            )

        return results
