from __future__ import annotations

import re

import structlog
from slack_bolt.async_app import AsyncApp

from ..core.coordinator import ThreadCoordinator
from ..core.permissions import PermissionManager

logger = structlog.get_logger()


def register_listeners(
    app: AsyncApp,
    coordinator: ThreadCoordinator,
    permission_mgr: PermissionManager,
) -> None:
    """Register all Slack event and action listeners."""

    @app.event("app_mention")
    async def handle_app_mention(event: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """Handle @bot mentions — starts or continues a thread."""
        text = event.get("text", "")
        # Strip the bot mention from the text
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            text = "Hello!"

        # Use thread_ts if this is a reply, otherwise use ts to start a new thread
        thread_ts = event.get("thread_ts", event.get("ts", ""))
        channel_id = event.get("channel", "")

        logger.info("listener.app_mention", channel=channel_id, thread_ts=thread_ts)
        await coordinator.handle_user_message(thread_ts, channel_id, text, say, client)

    @app.event("message")
    async def handle_message(event: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """Handle messages in threads the bot is participating in."""
        # Ignore bot messages (including our own)
        if event.get("bot_id") or event.get("subtype"):
            return

        # Only handle threaded replies (not top-level channel messages)
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        text = event.get("text", "")
        # Strip bot mentions from threaded replies too
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return

        channel_id = event.get("channel", "")

        logger.info("listener.thread_reply", channel=channel_id, thread_ts=thread_ts)
        await coordinator.handle_user_message(thread_ts, channel_id, text, say, client)

    @app.action("tool_allow")
    async def handle_tool_allow(ack: object, body: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """User clicked 'Allow' on a tool permission request."""
        await ack()  # type: ignore[operator]
        tool_use_id = body["actions"][0]["value"]
        thread_ts = body["message"].get("thread_ts", body["message"].get("ts", ""))

        logger.info("listener.tool_allowed", tool_use_id=tool_use_id)
        await coordinator.handle_tool_confirmation(tool_use_id, thread_ts, allowed=True, say=say, client=client)

    @app.action("tool_deny")
    async def handle_tool_deny(ack: object, body: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """User clicked 'Deny' on a tool permission request."""
        await ack()  # type: ignore[operator]
        tool_use_id = body["actions"][0]["value"]
        thread_ts = body["message"].get("thread_ts", body["message"].get("ts", ""))

        logger.info("listener.tool_denied", tool_use_id=tool_use_id)
        await coordinator.handle_tool_confirmation(tool_use_id, thread_ts, allowed=False, say=say, client=client)

    @app.action("tool_auto_approve")
    async def handle_auto_approve(ack: object, body: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """User clicked 'Auto-approve all' — enable auto-approve for this thread."""
        await ack()  # type: ignore[operator]
        tool_use_id = body["actions"][0]["value"]
        thread_ts = body["message"].get("thread_ts", body["message"].get("ts", ""))

        # Enable auto-approve for this thread
        await permission_mgr.set_auto_approve(thread_ts, enabled=True)
        await say(  # type: ignore[operator]
            text=":white_check_mark: Auto-approve enabled for this thread. All future tool requests will be automatically approved.",
            thread_ts=thread_ts,
        )

        # Also approve the current pending tool
        logger.info("listener.auto_approve_enabled", thread_ts=thread_ts, tool_use_id=tool_use_id)
        await coordinator.handle_tool_confirmation(tool_use_id, thread_ts, allowed=True, say=say, client=client)
