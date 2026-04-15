from __future__ import annotations

import asyncio

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


def _create_backend(settings: Settings) -> ClaudeCodeBackend:
    """Create the appropriate agent backend based on config."""
    if settings.default_backend == "claude-code":
        return ClaudeCodeBackend(model=settings.default_model)

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


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    logger.info("bot.starting", backend=settings.default_backend, model=settings.default_model)

    # Initialize database
    db = Database(settings.db_path)
    await db.initialize()

    # Initialize backend
    backend = _create_backend(settings)

    # Initialize core services
    coordinator = ThreadCoordinator(backend=backend, db=db)
    permission_mgr = PermissionManager(db=db)

    # Create and start Slack app
    app = create_slack_app(settings, coordinator, permission_mgr)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)

    logger.info("bot.ready", msg="Claude Slack Bot is running. Press Ctrl+C to stop.")
    await handler.start_async()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
