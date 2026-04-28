from __future__ import annotations

import asyncio

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .agent.backend import AgentBackend
from .agent.claude_code import ClaudeCodeBackend
from .agent.codex_cli import CodexCliBackend
from .agent.router import BackendRouter
from .config import Settings
from .core.coordinator import ThreadCoordinator
from .core.permissions import PermissionManager
from .db.database import Database
from .slack.app import create_slack_app
from .utils import watchdog
from .utils.logging import setup_logging

logger = structlog.get_logger()

# Watchdog: warn if no activity for this many seconds
WATCHDOG_TIMEOUT = 600  # 10 minutes — must be longer than the longest turn
WATCHDOG_CHECK_INTERVAL = 60  # check every minute


def touch_watchdog() -> None:
    """Back-compat shim. Prefer `from .utils.watchdog import touch`."""
    watchdog.touch()


def _create_backend(settings: Settings) -> AgentBackend:
    """Create all configured local backends and route per Slack thread."""
    backends: dict[str, AgentBackend] = {
        "claude-code": ClaudeCodeBackend(
            model=settings.default_model,
            cwd=settings.cwd or None,
            effort=settings.effort or None,
        ),
        "codex": CodexCliBackend(
            model=settings.codex_model,
            cwd=settings.cwd or None,
            effort=settings.effort or None,
            codex_bin=settings.codex_bin,
            bypass_approvals_and_sandbox=settings.codex_bypass_approvals_and_sandbox,
        ),
    }

    if settings.default_backend == "managed":
        import anthropic

        from .agent.managed import ManagedAgentBackend

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        backends["managed"] = ManagedAgentBackend(
            client=client,
            agent_id=settings.agent_id,
            agent_version=settings.agent_version,
        )

    if settings.default_backend == "messages":
        import anthropic

        from .agent.messages import MessagesBackend

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        backends["messages"] = MessagesBackend(client=client, model=settings.default_model)

    return BackendRouter(backends, default_backend_type=settings.default_backend)


async def _watchdog() -> None:
    """Log a warning if no Slack events arrive for an extended period."""
    while True:
        await asyncio.sleep(WATCHDOG_CHECK_INTERVAL)
        idle = watchdog.idle_seconds()
        if idle > WATCHDOG_TIMEOUT:
            logger.warning("watchdog.idle", idle_seconds=int(idle), msg="No Slack events — socket may be stale")


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    logger.info(
        "bot.starting",
        backend=settings.default_backend,
        model=settings.default_model,
        codex_model=settings.codex_model,
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
        watchdog.touch()
        await next()

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)

    # Restore any polls that were active before shutdown/restart
    await coordinator.restore_polls(app.client)

    # Start watchdog in background
    _wd = asyncio.create_task(_watchdog())  # noqa: RUF006

    logger.info("bot.ready", msg="Claude Slack Bot is running. Watchdog active.")
    await handler.start_async()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
