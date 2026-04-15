from __future__ import annotations

import os
import re
from typing import Any

import aiohttp
import structlog
from slack_bolt.async_app import AsyncApp

from ..core.coordinator import ThreadCoordinator
from ..core.permissions import PermissionManager

logger = structlog.get_logger()

DOWNLOAD_DIR = "/tmp/claude-slack-files"


async def _download_files(files: list[dict[str, Any]], client: Any) -> list[str]:
    """Download Slack file attachments to local disk, return file paths."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    paths: list[str] = []

    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue

        filename = f.get("name", f.get("id", "unknown"))
        local_path = os.path.join(DOWNLOAD_DIR, f"{f.get('id', 'f')}_{filename}")

        try:
            # Slack files need the bot token for auth
            token = client.token if hasattr(client, "token") else ""
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                    if resp.status == 200:
                        with open(local_path, "wb") as fp:
                            fp.write(await resp.read())
                        paths.append(local_path)
                        logger.info("file_download.ok", filename=filename, path=local_path)
                    else:
                        logger.warning("file_download.failed", filename=filename, status=resp.status)
        except Exception:
            logger.exception("file_download.error", filename=filename)

    return paths


def register_listeners(
    app: AsyncApp,
    coordinator: ThreadCoordinator,
    permission_mgr: PermissionManager,
) -> None:
    """Register all Slack event and action listeners."""

    @app.event("app_mention")
    async def handle_app_mention(event: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """Handle @bot mentions in channels (not DMs — those are handled by message handler)."""
        # Skip DMs — the message handler already covers them
        if event.get("channel_type") == "im":
            return

        text = event.get("text", "")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        files = event.get("files", [])
        if files:
            file_paths = await _download_files(files, client)
            if file_paths:
                file_note = "\n".join(f"[Attached file: {p}]" for p in file_paths)
                text = f"{text}\n\n{file_note}" if text else file_note

        if not text:
            text = "Hello!"

        thread_ts = event.get("thread_ts", event.get("ts", ""))
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")

        logger.info("listener.app_mention", channel=channel_id, thread_ts=thread_ts)
        await coordinator.handle_user_message(thread_ts, channel_id, text, say, client, user_id=user_id)

    @app.event("message")
    async def handle_message(event: dict, say: object, client: object) -> None:  # type: ignore[type-arg]
        """Handle messages in threads and DMs."""
        # Ignore bot messages (including our own)
        if event.get("bot_id") or event.get("subtype"):
            return

        text = event.get("text", "")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        # Download any attached files (images, etc.) and append paths to text
        files = event.get("files", [])
        if files:
            file_paths = await _download_files(files, client)
            if file_paths:
                file_note = "\n".join(f"[Attached file: {p}]" for p in file_paths)
                text = f"{text}\n\n{file_note}" if text else file_note

        if not text:
            return

        channel_id = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        user_id = event.get("user", "")

        # DMs: each message starts a thread (same model as channels)
        # User replies in-thread continue the session
        if channel_type == "im":
            thread_ts = event.get("thread_ts", event.get("ts", ""))
            logger.info("listener.dm", channel=channel_id, thread_ts=thread_ts)
            await coordinator.handle_user_message(thread_ts, channel_id, text, say, client, user_id=user_id)
            return

        # Channels: only handle threaded replies
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        logger.info("listener.thread_reply", channel=channel_id, thread_ts=thread_ts)
        await coordinator.handle_user_message(thread_ts, channel_id, text, say, client, user_id=user_id)

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
