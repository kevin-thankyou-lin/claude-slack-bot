from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from typing import AsyncIterator

import structlog

from .backend import EventType, SessionEvent
from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()


class CodexCliBackend:
    """Agent backend using the Codex CLI non-interactive runner.

    Each Slack thread maps to an in-memory Codex conversation. Codex CLI's
    persisted resume path is intentionally not required here; the bot keeps
    enough recent turn history to give every `codex exec` invocation context.
    """

    HISTORY_LIMIT = 20
    HISTORY_MESSAGE_CHAR_LIMIT = 2000
    HISTORY_TRANSCRIPT_CHAR_LIMIT = 12000
    STDOUT_READ_CHUNK_SIZE = 65536
    STDOUT_LINE_BYTE_LIMIT = 4 * 1024 * 1024
    IDLE_TIMEOUT = 43200
    _BENIGN_STDERR_PATTERNS = (
        "Reading additional input from stdin",
        "failed to record rollout items",
    )

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        cwd: str | None = None,
        effort: str | None = "high",
        codex_bin: str = "codex",
        bypass_approvals_and_sandbox: bool = True,
    ) -> None:
        self.model = model
        self.default_cwd = cwd
        self.effort = effort
        self.codex_bin = codex_bin
        self.bypass_approvals_and_sandbox = bypass_approvals_and_sandbox

        self._session_cwd: dict[str, str] = {}
        self._session_model: dict[str, str] = {}
        self._session_effort: dict[str, str] = {}
        self._history: dict[str, list[tuple[str, str]]] = {}
        self._codex_thread_ids: dict[str, str] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._last_active: dict[str, float] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    async def create_session(self, system_prompt: str | None = None) -> str:
        session_id = uuid.uuid4().hex
        if system_prompt:
            self._history[session_id] = [("system", system_prompt)]
        logger.info("codex_cli_backend.session_created", session_id=session_id)
        return session_id

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        self._session_cwd[session_id] = cwd

    async def set_session_model(self, session_id: str, model: str) -> None:
        self._session_model[session_id] = model

    async def set_session_effort(self, session_id: str, effort: str) -> None:
        self._session_effort[session_id] = effort

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        # Codex CLI approval behavior is configured per process invocation.
        return None

    def set_cc_session_id(self, session_id: str, cc_session_id: str) -> None:
        # Compatibility with the persisted column used by Claude Code.
        if cc_session_id:
            self._codex_thread_ids[session_id] = cc_session_id

    def get_cc_session_id(self, session_id: str) -> str:
        return self._codex_thread_ids.get(session_id, "")

    async def interrupt(self, session_id: str) -> None:
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            proc.terminate()
            logger.info("codex_cli_backend.interrupted", session_id=session_id)

    async def _reset_client(self, session_id: str) -> None:
        proc = self._processes.pop(session_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    async def shutdown(self) -> None:
        for sid in list(self._processes.keys()):
            await self._reset_client(sid)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        self._start_cleanup_loop()
        self._last_active[session_id] = time.monotonic()
        self._history.setdefault(session_id, []).append(("user", content))

        codex_path = shutil.which(self.codex_bin)
        if not codex_path:
            yield SessionEvent(type=EventType.ERROR, error_message=f"Codex CLI not found: {self.codex_bin}")
            return

        prompt = self._build_prompt(session_id, content)
        args = self._build_args(session_id, prompt)
        assistant_text_parts: list[str] = []
        stderr_parts: list[str] = []
        codex_error_parts: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                codex_path,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[session_id] = proc

            if proc.stdout is None:
                yield SessionEvent(type=EventType.ERROR, error_message="Codex CLI stdout unavailable")
                return

            stderr_task = asyncio.create_task(self._read_stderr(proc, stderr_parts))
            try:
                async for line in self._iter_stdout_lines(proc.stdout):
                    if not line:
                        continue
                    event = self._parse_json_event(session_id, line, codex_error_parts)
                    if event is None:
                        continue
                    if event.type == EventType.TEXT:
                        assistant_text_parts.append(event.text)
                    yield event

                return_code = await proc.wait()
                await stderr_task
            finally:
                self._processes.pop(session_id, None)

            assistant_text = "\n\n".join(part for part in assistant_text_parts if part).strip()
            if assistant_text:
                self._history.setdefault(session_id, []).append(("assistant", assistant_text))
                self._trim_history(session_id)
                if return_code != 0:
                    logger.warning(
                        "codex_cli_backend.nonzero_exit_after_text",
                        session_id=session_id,
                        return_code=return_code,
                        stderr=self._meaningful_stderr(stderr_parts)[-1000:],
                    )
                yield SessionEvent(type=EventType.TURN_END, is_final=True)
                return

            if return_code != 0:
                stderr = self._meaningful_stderr(stderr_parts)
                detail = "\n".join(part for part in [*codex_error_parts, stderr] if part).strip()
                logger.warning(
                    "codex_cli_backend.nonzero_exit_without_text",
                    session_id=session_id,
                    return_code=return_code,
                    codex_errors=codex_error_parts[-5:],
                    stderr=stderr[-1000:],
                )
                msg = f"Codex CLI exited with code {return_code}"
                if detail:
                    msg = f"{msg}: {detail[-2000:]}"
                yield SessionEvent(type=EventType.ERROR, error_message=msg)
                return

            yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except Exception as e:
            logger.exception("codex_cli_backend.send_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))
            await self._reset_client(session_id)

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    def _build_args(self, session_id: str, prompt: str) -> list[str]:
        args = ["exec", "--json", "--skip-git-repo-check"]
        if self.bypass_approvals_and_sandbox:
            args.append("--dangerously-bypass-approvals-and-sandbox")

        cwd = self._session_cwd.get(session_id) or self.default_cwd
        if cwd:
            args.extend(["-C", cwd])

        model = self._session_model.get(session_id) or self.model
        if model:
            args.extend(["-m", model])

        effort = self._session_effort.get(session_id) or self.effort
        if effort:
            args.extend(["-c", f'model_reasoning_effort="{effort}"'])

        args.append(prompt)
        return args

    async def _iter_stdout_lines(self, reader: asyncio.StreamReader) -> AsyncIterator[str]:
        buffer = bytearray()
        skipping_oversized_line = False

        while True:
            chunk = await reader.read(self.STDOUT_READ_CHUNK_SIZE)
            if not chunk:
                if buffer and not skipping_oversized_line:
                    yield buffer.decode(errors="replace").strip()
                return

            start = 0
            while start < len(chunk):
                newline_at = chunk.find(b"\n", start)
                end = len(chunk) if newline_at == -1 else newline_at
                segment = chunk[start:end]

                if skipping_oversized_line:
                    if newline_at == -1:
                        break
                    skipping_oversized_line = False
                    start = newline_at + 1
                    continue

                buffer.extend(segment)
                if len(buffer) > self.STDOUT_LINE_BYTE_LIMIT:
                    if self._looks_like_completed_command_line(buffer):
                        logger.warning(
                            "codex_cli_backend.skipped_oversized_command_event",
                            bytes=len(buffer),
                        )
                    else:
                        logger.warning(
                            "codex_cli_backend.skipped_oversized_stdout_line",
                            bytes=len(buffer),
                            prefix=bytes(buffer[:500]).decode(errors="replace"),
                        )
                    buffer.clear()
                    if newline_at == -1:
                        skipping_oversized_line = True
                        break

                if newline_at == -1:
                    break

                line = buffer.decode(errors="replace").strip()
                buffer.clear()
                start = newline_at + 1
                if line:
                    yield line

    @staticmethod
    def _looks_like_completed_command_line(line: bytes | bytearray) -> bool:
        return b'"type":"item.completed"' in line and b'"type":"command_execution"' in line

    def _build_prompt(self, session_id: str, content: str) -> str:
        history = self._history.get(session_id, [])
        prior = history[:-1][-self.HISTORY_LIMIT :]
        transcript = self._format_history(prior)
        if transcript:
            return f"{SYSTEM_PROMPT}\n\nPrior Slack thread context:\n{transcript}\n\nCurrent user message:\n{content}"
        return f"{SYSTEM_PROMPT}\n\nCurrent user message:\n{content}"

    def _format_history(self, prior: list[tuple[str, str]]) -> str:
        parts = []
        for role, text in prior:
            clipped = self._clip_text(text, self.HISTORY_MESSAGE_CHAR_LIMIT)
            parts.append(f"{role.upper()}: {clipped}")

        transcript = "\n".join(parts)
        return self._clip_text(transcript, self.HISTORY_TRANSCRIPT_CHAR_LIMIT)

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        half = max((limit - 34) // 2, 0)
        return f"{text[:half]}\n...[truncated]...\n{text[-half:]}"

    def _parse_json_event(
        self,
        session_id: str,
        line: str,
        codex_error_parts: list[str] | None = None,
    ) -> SessionEvent | None:
        if '"type":"item.completed"' in line and '"type":"command_execution"' in line:
            return None

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("codex_cli_backend.non_json_stdout", line=line[:500])
            return None

        event_type = payload.get("type")
        codex_error = self._extract_codex_error(payload)
        if codex_error:
            if codex_error_parts is not None:
                codex_error_parts.append(codex_error)
            logger.warning(
                "codex_cli_backend.error_event",
                session_id=session_id,
                event_type=event_type,
                error=codex_error[:1000],
            )
            return None

        if event_type == "thread.started":
            thread_id = str(payload.get("thread_id", ""))
            if thread_id:
                self._codex_thread_ids[session_id] = thread_id
            return None

        item = payload.get("item")
        if not isinstance(item, dict):
            return None

        item_type = item.get("type")
        if event_type == "item.started" and item_type == "command_execution":
            command = str(item.get("command", "command"))
            return SessionEvent(type=EventType.TOOL_ACTIVITY, tool_name=command)

        if event_type == "item.completed" and item_type == "agent_message":
            return SessionEvent(type=EventType.TEXT, text=str(item.get("text", "")))

        return None

    def _extract_codex_error(self, payload: dict[str, object]) -> str:
        event_type = payload.get("type")
        if event_type in {"error", "turn.failed", "turn.error"}:
            return self._stringify_error_payload(payload)

        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") in {"error", "turn.failed", "turn.error"}:
            return self._stringify_error_payload(item)

        if isinstance(payload.get("error"), (str, dict, list)):
            return self._stringify_error_payload(payload)

        return ""

    def _stringify_error_payload(self, payload: dict[str, object]) -> str:
        for key in ("message", "error_message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = self._stringify_error_payload(value)
                if nested:
                    return nested
            if isinstance(value, list):
                return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    async def _read_stderr(self, proc: asyncio.subprocess.Process, stderr_parts: list[str]) -> None:
        if proc.stderr is None:
            return
        async for raw_line in proc.stderr:
            line = raw_line.decode(errors="replace").strip()
            if line:
                stderr_parts.append(line)
                logger.debug("codex_cli_backend.stderr", line=line[:500])

    def _meaningful_stderr(self, stderr_parts: list[str]) -> str:
        lines = [line for line in stderr_parts if not any(pattern in line for pattern in self._BENIGN_STDERR_PATTERNS)]
        return "\n".join(lines).strip()

    def _trim_history(self, session_id: str) -> None:
        history = self._history.get(session_id, [])
        if len(history) > self.HISTORY_LIMIT * 2:
            self._history[session_id] = history[-self.HISTORY_LIMIT * 2 :]

    def _start_cleanup_loop(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_idle_sessions())

    async def _cleanup_idle_sessions(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [sid for sid, last in self._last_active.items() if now - last > self.IDLE_TIMEOUT]
            for sid in stale:
                await self._reset_client(sid)
                self._last_active.pop(sid, None)
                self._history.pop(sid, None)
                self._codex_thread_ids.pop(sid, None)
