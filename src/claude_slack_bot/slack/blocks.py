from __future__ import annotations

import json
from typing import Any


def build_permission_block(tool_name: str, tool_input: dict[str, object], tool_use_id: str) -> list[dict[str, Any]]:
    """Build Block Kit blocks for a tool permission request."""
    input_preview = json.dumps(tool_input, indent=2)
    if len(input_preview) > 1000:
        input_preview = input_preview[:997] + "..."

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":lock: *Permission Request*\nClaude wants to run `{tool_name}`:",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{input_preview}```",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Allow"},
                    "style": "primary",
                    "action_id": "tool_allow",
                    "value": tool_use_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "tool_deny",
                    "value": tool_use_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Auto-approve all"},
                    "action_id": "tool_auto_approve",
                    "value": tool_use_id,
                },
            ],
        },
    ]


def build_summary_block(summary: str, status: str = "completed") -> list[dict[str, Any]]:
    """Build Block Kit blocks for a conversation summary."""
    emoji = {
        "completed": ":white_check_mark:",
        "in_progress": ":hourglass_flowing_sand:",
        "blocked": ":no_entry_sign:",
    }.get(status, ":memo:")

    return [
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *Status: {status}*\n{summary}",
            },
        },
    ]
