from __future__ import annotations

from slack_bolt.async_app import AsyncApp

from ..config import Settings
from ..core.coordinator import ThreadCoordinator
from ..core.permissions import PermissionManager
from .listeners import register_listeners


def create_slack_app(
    settings: Settings,
    coordinator: ThreadCoordinator,
    permission_mgr: PermissionManager,
) -> AsyncApp:
    """Create and configure the Slack Bolt async app."""
    app = AsyncApp(token=settings.slack_bot_token)
    register_listeners(app, coordinator, permission_mgr)
    return app
