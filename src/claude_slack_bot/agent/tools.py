from __future__ import annotations

from typing import Any

CUSTOM_TOOLS: list[dict[str, Any]] = [
    {
        "type": "custom",
        "name": "generate_image",
        "description": (
            "Generate an image by writing and executing a Python script. "
            "The script should save the image to /tmp/ with a descriptive filename. "
            "The image will be automatically uploaded to the Slack thread. "
            "Use matplotlib, PIL, or similar libraries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the image should depict",
                },
                "script": {
                    "type": "string",
                    "description": "Python script that generates and saves the image to /tmp/",
                },
                "output_path": {
                    "type": "string",
                    "description": "Path where the script saves the image (e.g., /tmp/chart.png)",
                },
            },
            "required": ["description", "script", "output_path"],
        },
    },
    {
        "type": "custom",
        "name": "create_video",
        "description": (
            "Create a short MP4 video by writing and executing a Python script. "
            "The script should save the video to /tmp/ as an MP4 file. "
            "The video will be automatically uploaded to the Slack thread. "
            "Use matplotlib.animation, moviepy, or ffmpeg."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the video should show",
                },
                "script": {
                    "type": "string",
                    "description": "Python script that generates and saves the video to /tmp/",
                },
                "output_path": {
                    "type": "string",
                    "description": "Path where the script saves the video (e.g., /tmp/animation.mp4)",
                },
            },
            "required": ["description", "script", "output_path"],
        },
    },
    {
        "type": "custom",
        "name": "post_summary",
        "description": (
            "Post a formatted summary of the conversation so far to the Slack thread. "
            "Use this after completing a significant milestone or when the user asks for status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-3 sentence summary of what was accomplished",
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "in_progress", "blocked"],
                    "description": "Current task status",
                },
            },
            "required": ["summary", "status"],
        },
    },
]
