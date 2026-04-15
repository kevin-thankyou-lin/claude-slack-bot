from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from typing import Any, AsyncIterator

import structlog

from .backend import EventType, SessionEvent
from .prompts import SYSTEM_PROMPT

logger = structlog.get_logger()


class ClaudeCodeBackend:
    """Agent backend that spawns the ``claude`` CLI as a subprocess.

    Uses the user's existing Claude subscription (Pro/Max/Team/Enterprise)
    so no API key is needed.  Each Slack thread maps to one Claude Code
    session that is resumed with ``--resume <session_id>`` for multi-turn
    conversations.

    CLI flags used:
      --print              non-interactive, single response
      --output-format json structured output with session_id
      --resume <id>        continue an existing session
      --dangerously-skip-permissions   when auto-approve is on
      --append-system-prompt           inject our Slack-specific instructions
    """

    def __init__(
        self,
        *,
        model: str = "sonnet",
        timeout_seconds: int = 300,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        # session_id (our internal) -> claude session_id (from CLI)
        self._cli_sessions: dict[str, str] = {}
        self._auto_approve: set[str] = set()

        claude_path = shutil.which("claude")
        if claude_path is None:
            raise RuntimeError(
                "claude CLI not found on PATH. Install it first: https://docs.anthropic.com/en/docs/claude-code"
            )
        self._claude_bin = claude_path

    async def create_session(self, system_prompt: str | None = None) -> str:
        """Create a logical session.  The real CLI session is created on first message."""
        session_id = uuid.uuid4().hex
        logger.info("claude_code_backend.session_created", session_id=session_id)
        return session_id

    def set_auto_approve(self, session_id: str, *, enabled: bool) -> None:
        if enabled:
            self._auto_approve.add(session_id)
        else:
            self._auto_approve.discard(session_id)

    async def send_message(self, session_id: str, content: str) -> AsyncIterator[SessionEvent]:
        """Send a message to claude CLI and yield response events."""
        cmd = self._build_command(session_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=content.encode()),
                timeout=self.timeout_seconds,
            )

            stdout = stdout_bytes.decode(errors="replace").strip()
            stderr = stderr_bytes.decode(errors="replace").strip()

            if proc.returncode != 0:
                error_msg = stderr or f"claude exited with code {proc.returncode}"
                logger.error("claude_code_backend.error", error=error_msg, returncode=proc.returncode)
                yield SessionEvent(type=EventType.ERROR, error_message=error_msg)
                return

            # Parse JSON result
            result = self._parse_result(stdout)
            if result is None:
                # Fallback: treat raw stdout as text
                yield SessionEvent(type=EventType.TEXT, text=stdout)
                yield SessionEvent(type=EventType.TURN_END, is_final=True)
                return

            # Store the CLI session ID for future --resume calls
            cli_session_id = result.get("session_id", "")
            if cli_session_id:
                self._cli_sessions[session_id] = cli_session_id

            # Check for permission denials
            permission_denials = result.get("permission_denials", [])
            if permission_denials:
                for denial in permission_denials:
                    yield SessionEvent(
                        type=EventType.TOOL_CONFIRMATION_NEEDED,
                        tool_use_id=str(denial) if isinstance(denial, str) else json.dumps(denial),
                        tool_name="permission_denied",
                        tool_input={"details": denial},
                    )

            # Extract the response text
            response_text = result.get("result", "")
            if response_text:
                yield SessionEvent(type=EventType.TEXT, text=response_text)

            yield SessionEvent(type=EventType.TURN_END, is_final=True)

        except asyncio.TimeoutError:
            logger.error("claude_code_backend.timeout", session_id=session_id)
            yield SessionEvent(
                type=EventType.ERROR, error_message=f"Claude Code timed out after {self.timeout_seconds}s"
            )

        except Exception as e:
            logger.exception("claude_code_backend.unexpected_error", session_id=session_id)
            yield SessionEvent(type=EventType.ERROR, error_message=str(e))

    async def send_tool_result(self, session_id: str, tool_use_id: str, result: str) -> AsyncIterator[SessionEvent]:
        """Not used for CLI backend — tools are handled by Claude Code internally."""
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    async def send_tool_confirmation(
        self, session_id: str, tool_use_id: str, allowed: bool
    ) -> AsyncIterator[SessionEvent]:
        """Not directly applicable — auto-approve is handled via CLI flags."""
        if allowed:
            self.set_auto_approve(session_id, enabled=True)
        yield SessionEvent(type=EventType.TURN_END, is_final=True)

    def _build_command(self, session_id: str) -> list[str]:
        cmd = [
            self._claude_bin,
            "--print",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--append-system-prompt",
            SYSTEM_PROMPT,
        ]

        # Resume existing session if we have a CLI session ID
        cli_session_id = self._cli_sessions.get(session_id)
        if cli_session_id:
            cmd.extend(["--resume", cli_session_id])

        # Auto-approve: skip all permission prompts
        if session_id in self._auto_approve:
            cmd.append("--dangerously-skip-permissions")

        return cmd

    def _parse_result(self, stdout: str) -> dict[str, Any] | None:
        """Parse the JSON result from claude CLI output."""
        # The output may contain multiple JSON lines (stream-json) or a single JSON object
        # With --output-format json, it's a single JSON object
        for raw_line in stdout.strip().split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get("type") == "result":
                    return data
            except json.JSONDecodeError:
                continue
        return None
