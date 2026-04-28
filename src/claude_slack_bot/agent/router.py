from __future__ import annotations

from typing import Any, AsyncIterator

from .backend import SessionEvent

_ALIASES = {
    "claude": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "codex-cli": "codex",
    "openai": "codex",
}


def normalize_backend_type(backend_type: str) -> str:
    key = backend_type.strip().lower().replace("_", "-")
    return _ALIASES.get(key, key)


class BackendRouter:
    """Routes AgentBackend calls to one of several concrete backends."""

    def __init__(self, backends: dict[str, Any], default_backend_type: str) -> None:
        self.backends = {normalize_backend_type(name): backend for name, backend in backends.items()}
        self.default_backend_type = normalize_backend_type(default_backend_type)
        self._session_backend: dict[str, str] = {}
        if self.default_backend_type not in self.backends:
            raise ValueError(f"Unknown default backend: {default_backend_type}")

    def available_backend_types(self) -> tuple[str, ...]:
        return tuple(sorted(self.backends))

    def default_model_for_backend(self, backend_type: str) -> str:
        backend = self._backend_by_type(normalize_backend_type(backend_type))
        return str(getattr(backend, "model", ""))

    def register_session(self, session_id: str, backend_type: str) -> None:
        normalized = normalize_backend_type(backend_type)
        if normalized in self.backends:
            self._session_backend[session_id] = normalized

    async def create_session(self, system_prompt: str | None = None, backend_type: str | None = None) -> str:
        normalized = normalize_backend_type(backend_type or self.default_backend_type)
        backend = self._backend_by_type(normalized)
        session_id = await backend.create_session(system_prompt)
        self._session_backend[session_id] = normalized
        return session_id

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        async for event in self._backend_for_session(session_id).send_message(session_id, content):
            yield event

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        async for event in self._backend_for_session(session_id).send_tool_result(session_id, tool_use_id, result):
            yield event

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        async for event in self._backend_for_session(session_id).send_tool_confirmation(
            session_id, tool_use_id, allowed
        ):
            yield event

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "set_session_cwd"):
            await backend.set_session_cwd(session_id, cwd)

    async def set_session_model(self, session_id: str, model: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "set_session_model"):
            await backend.set_session_model(session_id, model)

    async def set_session_effort(self, session_id: str, effort: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "set_session_effort"):
            await backend.set_session_effort(session_id, effort)

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "set_auto_approve"):
            backend.set_auto_approve(session_id, enabled=enabled)

    async def interrupt(self, session_id: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "interrupt"):
            await backend.interrupt(session_id)

    async def _reset_client(self, session_id: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "_reset_client"):
            await backend._reset_client(session_id)

    def set_cc_session_id(self, session_id: str, cc_session_id: str) -> None:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "set_cc_session_id"):
            backend.set_cc_session_id(session_id, cc_session_id)

    def get_cc_session_id(self, session_id: str) -> str:
        backend = self._backend_for_session(session_id)
        if hasattr(backend, "get_cc_session_id"):
            return str(backend.get_cc_session_id(session_id))
        return ""

    async def shutdown(self) -> None:
        for backend in self.backends.values():
            if hasattr(backend, "shutdown"):
                await backend.shutdown()

    def _backend_by_type(self, backend_type: str) -> Any:
        try:
            return self.backends[backend_type]
        except KeyError as exc:
            available = ", ".join(self.available_backend_types())
            raise ValueError(f"Unknown backend `{backend_type}`. Available: {available}") from exc

    def _backend_for_session(self, session_id: str) -> Any:
        backend_type = self._session_backend.get(session_id, self.default_backend_type)
        return self._backend_by_type(backend_type)
