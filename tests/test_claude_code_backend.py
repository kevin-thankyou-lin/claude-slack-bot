from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from claude_slack_bot.agent.backend import EventType
from claude_slack_bot.agent.claude_code import ClaudeCodeBackend


def _mock_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> AsyncMock:
    proc = AsyncMock()
    proc.communicate.return_value = (stdout.encode(), stderr.encode())
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_create_session() -> None:
    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()
    session_id = await backend.create_session()
    assert session_id
    assert len(session_id) == 32  # uuid hex


@pytest.mark.asyncio
async def test_send_message_success() -> None:
    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "Hello from Claude!",
            "session_id": "cli-sess-123",
            "is_error": False,
            "permission_denials": [],
        }
    )

    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()

    session_id = await backend.create_session()

    with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=result_json)):
        events = []
        async for event in backend.send_message(session_id, "Hi"):
            events.append(event)

    assert len(events) == 2
    assert events[0].type == EventType.TEXT
    assert events[0].text == "Hello from Claude!"
    assert events[1].type == EventType.TURN_END

    # Should have stored the CLI session ID for resume
    assert backend._cli_sessions[session_id] == "cli-sess-123"


@pytest.mark.asyncio
async def test_send_message_error() -> None:
    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()

    session_id = await backend.create_session()

    with patch("asyncio.create_subprocess_exec", return_value=_mock_process(returncode=1, stderr="auth failed")):
        events = []
        async for event in backend.send_message(session_id, "Hi"):
            events.append(event)

    assert len(events) == 1
    assert events[0].type == EventType.ERROR
    assert "auth failed" in events[0].error_message


@pytest.mark.asyncio
async def test_resume_uses_session_id() -> None:
    result_json = json.dumps(
        {
            "type": "result",
            "result": "resumed!",
            "session_id": "cli-sess-456",
            "permission_denials": [],
        }
    )

    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()

    session_id = await backend.create_session()
    # Simulate a previous session
    backend._cli_sessions[session_id] = "cli-sess-456"

    with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=result_json)) as mock_exec:
        events = []
        async for event in backend.send_message(session_id, "continue"):
            events.append(event)

        # Check that --resume was passed
        call_args = mock_exec.call_args[0]
        assert "--resume" in call_args
        assert "cli-sess-456" in call_args


@pytest.mark.asyncio
async def test_auto_approve_adds_flag() -> None:
    result_json = json.dumps(
        {
            "type": "result",
            "result": "done",
            "session_id": "cli-sess-789",
            "permission_denials": [],
        }
    )

    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()

    session_id = await backend.create_session()
    backend.set_auto_approve(session_id, enabled=True)

    with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout=result_json)) as mock_exec:
        events = []
        async for event in backend.send_message(session_id, "do something"):
            events.append(event)

        call_args = mock_exec.call_args[0]
        assert "--dangerously-skip-permissions" in call_args


@pytest.mark.asyncio
async def test_fallback_plain_text() -> None:
    with patch("shutil.which", return_value="/usr/bin/claude"):
        backend = ClaudeCodeBackend()

    session_id = await backend.create_session()

    # Non-JSON output should be returned as plain text
    with patch("asyncio.create_subprocess_exec", return_value=_mock_process(stdout="just plain text")):
        events = []
        async for event in backend.send_message(session_id, "Hi"):
            events.append(event)

    assert len(events) == 2
    assert events[0].type == EventType.TEXT
    assert events[0].text == "just plain text"
