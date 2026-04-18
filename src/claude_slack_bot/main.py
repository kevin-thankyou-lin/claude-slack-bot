from __future__ import annotations

import asyncio
import os
import sys
import time

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .agent.claude_code import ClaudeCodeBackend
from .config import Settings
from .core.coordinator import ThreadCoordinator
from .core.permissions import PermissionManager
from .db.database import Database
from .slack.app import create_slack_app
from .utils.logging import setup_logging

logger = structlog.get_logger()

# Watchdog: restart if no activity for this many seconds
WATCHDOG_TIMEOUT = 600  # 10 minutes — must be longer than the longest turn
WATCHDOG_CHECK_INTERVAL = 60  # check every minute

# Shared timestamp updated by the Slack listener middleware
_last_event_time: float = time.monotonic()


def touch_watchdog() -> None:
    """Called by Slack middleware on every event to prove the connection is alive."""
    global _last_event_time  # noqa: PLW0603
    _last_event_time = time.monotonic()


def _create_backend(settings: Settings) -> ClaudeCodeBackend:
    """Create the appropriate agent backend based on config."""
    if settings.default_backend == "claude-code":
        return ClaudeCodeBackend(
            model=settings.default_model,
            cwd=settings.cwd or None,
            effort=settings.effort or None,
        )

    if settings.default_backend == "managed":
        import anthropic

        from .agent.managed import ManagedAgentBackend

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        return ManagedAgentBackend(client=client, agent_id=settings.agent_id, agent_version=settings.agent_version)  # type: ignore[return-value]

    # Default: messages API
    import anthropic

    from .agent.messages import MessagesBackend

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return MessagesBackend(client=client, model=settings.default_model)  # type: ignore[return-value]


async def _watchdog() -> None:
    """Monitor the Slack connection and restart the process if it goes silent."""
    while True:
        await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)
        idle = time.monotonic() - _last_event_time
        if idle > WATCHDOG_TIMEOUT:
            logger.error("watchdog.timeout", idle_seconds=int(idle))
            logger.info("watchdog.restarting")
            # Re-exec the process — clean restart, same args
            os.execv(sys.executable, [sys.executable, "-m", "claude_slack_bot.main"])


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    logger.info(
        "bot.starting",
        backend=settings.default_backend,
        model=settings.default_model,
        effort=settings.effort,
    )

    # Initialize database
    db = Database(settings.db_path)
    await db.initialize()

    # Initialize backend
    backend = _create_backend(settings)

    # Initialize core services
    coordinator = ThreadCoordinator(backend=backend, db=db, projects_dir=settings.projects_dir)
    permission_mgr = PermissionManager(db=db)

    # Create and start Slack app with watchdog middleware
    app = create_slack_app(settings, coordinator, permission_mgr)

    # Add middleware that touches the watchdog on every event
    @app.middleware
    async def watchdog_middleware(body: object, next: object) -> None:
        touch_watchdog()
        await next()

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)

    # Start watchdog in background
    _wd = asyncio.create_task(_watchdog())  # noqa: RUF006

    logger.info("bot.ready", msg="Claude Slack Bot is running. Watchdog active.")
    await handler.start_async()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
