from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_slack_bot.agent.backend import EventType
from claude_slack_bot.agent.messages import MessagesBackend


def _make_text_response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "end_turn"
    return response


def _make_tool_use_response(tool_id: str, tool_name: str, tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = tool_name
    block.input = tool_input

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    return response


@pytest.mark.asyncio
async def test_create_session() -> None:
    client = AsyncMock()
    backend = MessagesBackend(client)
    session_id = await backend.create_session()
    assert session_id  # non-empty string
    assert session_id in backend._sessions


@pytest.mark.asyncio
async def test_send_message_text_response() -> None:
    client = AsyncMock()
    client.messages.create.return_value = _make_text_response("Hello!")

    backend = MessagesBackend(client)
    session_id = await backend.create_session()

    events = []
    async for event in backend.send_message(session_id, "Hi"):
        events.append(event)

    assert len(events) == 2
    assert events[0].type == EventType.TEXT
    assert events[0].text == "Hello!"
    assert events[1].type == EventType.TURN_END


@pytest.mark.asyncio
async def test_send_message_tool_confirmation_needed() -> None:
    client = AsyncMock()
    client.messages.create.return_value = _make_tool_use_response("tool-1", "bash", {"command": "ls"})

    backend = MessagesBackend(client)
    session_id = await backend.create_session()

    events = []
    async for event in backend.send_message(session_id, "list files"):
        events.append(event)

    # bash requires confirmation
    assert len(events) == 1
    assert events[0].type == EventType.TOOL_CONFIRMATION_NEEDED
    assert events[0].tool_name == "bash"


@pytest.mark.asyncio
async def test_send_message_safe_tool_no_confirmation() -> None:
    client = AsyncMock()
    client.messages.create.return_value = _make_tool_use_response("tool-2", "read_file", {"path": "/tmp/test.txt"})

    backend = MessagesBackend(client)
    session_id = await backend.create_session()

    events = []
    async for event in backend.send_message(session_id, "read a file"):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == EventType.TOOL_USE
    assert events[0].tool_name == "read_file"
