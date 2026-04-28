from __future__ import annotations

import asyncio

import pytest

from claude_slack_bot.agent.backend import EventType
from claude_slack_bot.agent.codex_cli import CodexCliBackend


@pytest.mark.asyncio
async def test_create_session() -> None:
    backend = CodexCliBackend()
    session_id = await backend.create_session()
    assert session_id
    assert len(session_id) == 32


def test_parse_thread_started() -> None:
    backend = CodexCliBackend()
    event = backend._parse_json_event("s1", '{"type":"thread.started","thread_id":"codex-thread"}')
    assert event is None
    assert backend.get_cc_session_id("s1") == "codex-thread"


def test_parse_command_activity() -> None:
    backend = CodexCliBackend()
    event = backend._parse_json_event(
        "s1",
        '{"type":"item.started","item":{"type":"command_execution","command":"/bin/bash -lc pwd"}}',
    )
    assert event is not None
    assert event.type == EventType.TOOL_ACTIVITY
    assert event.tool_name == "/bin/bash -lc pwd"


def test_parse_agent_message() -> None:
    backend = CodexCliBackend()
    event = backend._parse_json_event(
        "s1",
        '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}',
    )
    assert event is not None
    assert event.type == EventType.TEXT
    assert event.text == "hello"


@pytest.mark.asyncio
async def test_iter_stdout_lines_skips_oversized_command_event() -> None:
    backend = CodexCliBackend()
    backend.STDOUT_LINE_BYTE_LIMIT = 80
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"'
        + (b"x" * 200)
        + b'"}}\n'
        + b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
    )
    reader.feed_eof()

    lines = [line async for line in backend._iter_stdout_lines(reader)]

    assert lines == ['{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}']


def test_parse_error_event_collects_error_detail() -> None:
    backend = CodexCliBackend()
    errors: list[str] = []
    event = backend._parse_json_event(
        "s1",
        '{"type":"turn.failed","error":{"message":"context limit exceeded"}}',
        errors,
    )
    assert event is None
    assert errors == ["context limit exceeded"]


def test_parse_completed_command_execution_skips_large_payload() -> None:
    backend = CodexCliBackend()
    event = backend._parse_json_event(
        "s1",
        '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"large"}}',
    )
    assert event is None


def test_meaningful_stderr_filters_codex_noise() -> None:
    backend = CodexCliBackend()
    stderr = backend._meaningful_stderr(
        [
            "Reading additional input from stdin...",
            "2026-04-27T07:21:02Z ERROR codex_core::session: failed to record rollout items: thread x not found",
            "real error",
        ]
    )
    assert stderr == "real error"


def test_build_prompt_caps_history() -> None:
    backend = CodexCliBackend()
    backend._history["s1"] = [
        ("assistant", "x" * (backend.HISTORY_TRANSCRIPT_CHAR_LIMIT * 2)),
        ("user", "current"),
    ]
    prompt = backend._build_prompt("s1", "current")
    assert "...[truncated]..." in prompt
    assert len(prompt) < backend.HISTORY_TRANSCRIPT_CHAR_LIMIT + 5000


def test_build_args_uses_codex_options() -> None:
    backend = CodexCliBackend(model="gpt-test", cwd="/tmp/project", effort="medium")
    args = backend._build_args("s1", "prompt")
    assert args[:3] == ["exec", "--json", "--skip-git-repo-check"]
    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert args[args.index("-C") + 1] == "/tmp/project"
    assert args[args.index("-m") + 1] == "gpt-test"
    assert 'model_reasoning_effort="medium"' in args
    assert args[-1] == "prompt"
