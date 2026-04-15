from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_code_sdk import AssistantMessage, ResultMessage, SystemMessage
from claude_code_sdk.types import TextBlock

from claude_slack_bot.agent.backend import EventType
from claude_slack_bot.agent.claude_code import ClaudeCodeBackend


def _make_assistant_msg(text: str) -> MagicMock:
    msg = MagicMock(spec=AssistantMessage)
    block = MagicMock(spec=TextBlock)
    block.text = text
    msg.content = [block]
    return msg


def _make_result_msg(result: str | None = None) -> MagicMock:
    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    return msg


def _make_system_msg() -> MagicMock:
    return MagicMock(spec=SystemMessage)


@pytest.mark.asyncio
async def test_create_session() -> None:
    backend = ClaudeCodeBackend()
    session_id = await backend.create_session()
    assert session_id
    assert len(session_id) == 32


@pytest.mark.asyncio
async def test_send_message_success() -> None:
    backend = ClaudeCodeBackend()

    mock_client = AsyncMock()
    mock_client.query = AsyncMock()

    async def mock_receive() -> None:  # type: ignore[return-type]
        yield _make_system_msg()
        yield _make_assistant_msg("Hello from Claude!")
        yield _make_result_msg("Hello from Claude!")

    mock_client.receive_response = mock_receive
    backend._client = mock_client

    session_id = await backend.create_session()
    events = []
    async for event in backend.send_message(session_id, "Hi"):
        events.append(event)

    assert len(events) == 2
    assert events[0].type == EventType.TEXT
    assert events[0].text == "Hello from Claude!"
    assert events[1].type == EventType.TURN_END

    mock_client.query.assert_called_once_with("Hi", session_id=session_id)


@pytest.mark.asyncio
async def test_send_message_error_resets_client() -> None:
    backend = ClaudeCodeBackend()

    mock_client = AsyncMock()
    mock_client.query = AsyncMock(side_effect=RuntimeError("connection lost"))
    mock_client.disconnect = AsyncMock()
    backend._client = mock_client

    session_id = await backend.create_session()
    events = []
    async for event in backend.send_message(session_id, "Hi"):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == EventType.ERROR
    assert "connection lost" in events[0].error_message
    assert backend._client is None


@pytest.mark.asyncio
async def test_auto_approve_tracking() -> None:
    backend = ClaudeCodeBackend()
    session_id = await backend.create_session()

    assert session_id not in backend._auto_approve
    backend.set_auto_approve(session_id, enabled=True)
    assert session_id in backend._auto_approve
    backend.set_auto_approve(session_id, enabled=False)
    assert session_id not in backend._auto_approve


@pytest.mark.asyncio
async def test_shutdown() -> None:
    backend = ClaudeCodeBackend()
    mock_client = AsyncMock()
    backend._client = mock_client

    await backend.shutdown()
    mock_client.disconnect.assert_called_once()
    assert backend._client is None
