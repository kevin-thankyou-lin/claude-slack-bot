from __future__ import annotations

from typing import AsyncIterator

import pytest

from claude_slack_bot.agent.backend import EventType, SessionEvent
from claude_slack_bot.agent.router import BackendRouter, normalize_backend_type


class FakeBackend:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.cwd_by_session: dict[str, str] = {}

    async def create_session(self, system_prompt: str | None = None) -> str:
        return f"{self.prefix}-session"

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TEXT, text=f"{self.prefix}:{content}")
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        self.cwd_by_session[session_id] = cwd


def test_normalize_backend_type_aliases() -> None:
    assert normalize_backend_type("claude") == "claude-code"
    assert normalize_backend_type("codex-cli") == "codex"


def test_router_exposes_backend_default_model() -> None:
    codex = FakeBackend("codex")
    codex.model = "gpt-5.4"
    router = BackendRouter({"claude-code": FakeBackend("claude"), "codex": codex}, "claude-code")

    assert router.default_model_for_backend("codex") == "gpt-5.4"


@pytest.mark.asyncio
async def test_router_routes_created_session() -> None:
    router = BackendRouter(
        {"claude-code": FakeBackend("claude"), "codex": FakeBackend("codex")},
        default_backend_type="claude-code",
    )
    session_id = await router.create_session(backend_type="codex")

    events = []
    async for event in router.send_message(session_id, "hi"):
        events.append(event)

    assert events[0].type == EventType.TEXT
    assert events[0].text == "codex:hi"


@pytest.mark.asyncio
async def test_router_registers_existing_session() -> None:
    codex = FakeBackend("codex")
    router = BackendRouter({"claude-code": FakeBackend("claude"), "codex": codex}, "claude-code")
    router.register_session("old-session", "codex")
    await router.set_session_cwd("old-session", "/tmp/repo")

    assert codex.cwd_by_session["old-session"] == "/tmp/repo"
