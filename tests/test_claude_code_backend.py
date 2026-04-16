from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_code_sdk import ResultMessage
from claude_code_sdk.types import StreamEvent

from claude_slack_bot.agent.backend import EventType
from claude_slack_bot.agent.claude_code import ClaudeCodeBackend


def _make_stream_delta(text: str) -> MagicMock:
    msg = MagicMock(spec=StreamEvent)
    msg.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    return msg


def _make_result_msg(result: str | None = None, session_id: str = "cc-test-123") -> MagicMock:
    msg = MagicMock(spec=ResultMessage)
    msg.result = result
    msg.session_id = session_id
    return msg


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
        yield _make_stream_delta("Hello ")
        yield _make_stream_delta("from Claude!")
        yield _make_result_msg("Hello from Claude!")

    mock_client.receive_response = mock_receive

    session_id = await backend.create_session()
    backend._clients[session_id] = mock_client

    events = []
    async for event in backend.send_message(session_id, "Hi"):
        events.append(event)

    assert len(events) == 3
    assert events[0].type == EventType.TEXT_DELTA
    assert events[0].text == "Hello "
    assert events[1].type == EventType.TEXT_DELTA
    assert events[1].text == "from Claude!"
    assert events[2].type == EventType.TURN_END

    mock_client.query.assert_called_once_with("Hi", session_id=session_id)


@pytest.mark.asyncio
async def test_send_message_error_resets_client() -> None:
    backend = ClaudeCodeBackend()

    mock_client = AsyncMock()
    mock_client.query = AsyncMock(side_effect=RuntimeError("connection lost"))
    mock_client.disconnect = AsyncMock()

    session_id = await backend.create_session()
    backend._clients[session_id] = mock_client

    events = []
    async for event in backend.send_message(session_id, "Hi"):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == EventType.ERROR
    assert "connection lost" in events[0].error_message
    assert session_id not in backend._clients


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
    session_id = await backend.create_session()
    backend._clients[session_id] = mock_client

    await backend.shutdown()
    mock_client.disconnect.assert_called_once()
    assert len(backend._clients) == 0


@pytest.mark.asyncio
async def test_session_cwd() -> None:
    backend = ClaudeCodeBackend()
    session_id = await backend.create_session()
    await backend.set_session_cwd(session_id, "/home/user/project")
    assert backend._session_cwd[session_id] == "/home/user/project"


@pytest.mark.asyncio
async def test_parallel_sessions_isolated() -> None:
    """Two sessions should use separate clients."""
    backend = ClaudeCodeBackend()

    mock_a = AsyncMock()
    mock_b = AsyncMock()

    session_a = await backend.create_session()
    session_b = await backend.create_session()

    backend._clients[session_a] = mock_a
    backend._clients[session_b] = mock_b

    assert backend._clients[session_a] is not backend._clients[session_b]
