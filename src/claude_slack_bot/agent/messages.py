from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

import anthropic
import structlog

from .backend import EventType, SessionEvent
from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()

# Tools available to the Messages API backend
MESSAGES_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": "Execute a bash command and return its output. Use for running code, installing packages, file operations, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write to"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
            },
            "required": ["path"],
        },
    },
]

# Tools that require user confirmation before execution
CONFIRMATION_REQUIRED = {"bash"}


class MessagesBackend:
    """Agent backend using the Anthropic Messages API with a local agentic loop."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str = "claude-sonnet-4-6") -> None:
        self.client = client
        self.model = model
        # session_id -> list of message dicts (role + content)
        self._sessions: dict[str, list[dict[str, Any]]] = {}
        # session_id -> system prompt
        self._system_prompts: dict[str, str] = {}
        # session_id -> pending tool uses waiting for confirmation
        self._pending_tools: dict[str, dict[str, dict[str, Any]]] = {}

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = []
        self._system_prompts[session_id] = system_prompt or SYSTEM_PROMPT
        self._pending_tools[session_id] = {}
        logger.info("messages_backend.session_created", session_id=session_id)
        return session_id

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        messages = self._sessions[session_id]
        messages.append({"role": "user", "content": content})

        async for event in self._run_turn(session_id):
            yield event

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        messages = self._sessions[session_id]
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result}],
            }
        )

        async for event in self._run_turn(session_id):
            yield event

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        pending = self._pending_tools.get(session_id, {}).pop(tool_use_id, None)
        if pending is None:
            yield SessionEvent(type=EventType.ERROR, error_message=f"No pending tool {tool_use_id}")
            return

        if not allowed:
            # Send a denial as a tool result
            messages = self._sessions[session_id]
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "User denied this action.",
                            "is_error": True,
                        }
                    ],
                }
            )
            async for event in self._run_turn(session_id):
                yield event
            return

        # Yield the approved tool_use so the coordinator knows to execute it
        yield SessionEvent(
            type=EventType.TOOL_USE,
            tool_use_id=tool_use_id,
            tool_name=pending["name"],
            tool_input=pending["input"],
        )

    async def _run_turn(self, session_id: str) -> AsyncIterator[SessionEvent]:
        messages = self._sessions[session_id]
        system_prompt = self._system_prompts[session_id]

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=MESSAGES_TOOLS,
            )
        except anthropic.APIError as e:
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            return

        # Build the assistant message content for conversation history
        assistant_content: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
                yield SessionEvent(type=EventType.TEXT, text=block.text)
            elif block.type == "tool_use":
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

                if block.name in CONFIRMATION_REQUIRED:
                    # Store pending and ask for confirmation
                    self._pending_tools.setdefault(session_id, {})[block.id] = {
                        "name": block.name,
                        "input": block.input,
                    }
                    yield SessionEvent(
                        type=EventType.TOOL_CONFIRMATION_NEEDED,
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input if isinstance(block.input, dict) else {},
                    )
                else:
                    yield SessionEvent(
                        type=EventType.TOOL_USE,
                        tool_use_id=block.id,
                        tool_name=block.name,
                        tool_input=block.input if isinstance(block.input, dict) else {},
                    )

        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason == "end_turn":
            yield SessionEvent(type=EventType.TURN_END, is_final=True)
        # If stop_reason is "tool_use", the coordinator handles executing tools
        # and calling send_tool_result to continue the loop.
