from __future__ import annotations

import asyncio

import anthropic
import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from .agent.messages import MessagesBackend
from .config import Settings
from .core.coordinator import ThreadCoordinator
from .core.permissions import PermissionManager
from .db.database import Database
from .slack.app import create_slack_app
from .utils.logging import setup_logging

logger = structlog.get_logger()


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    logger.info("bot.starting", backend=settings.default_backend, model=settings.default_model)

    # Initialize database
    db = Database(settings.db_path)
    await db.initialize()

    # Initialize Anthropic backend
    anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    backend = MessagesBackend(client=anthropic_client, model=settings.default_model)

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
