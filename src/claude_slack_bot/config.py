from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    agent_id: str = ""
    agent_version: int = 1
    default_model: str = "claude-opus-4-7"  # Default Claude model
    codex_model: str = "gpt-5.4"  # Default Codex model
    effort: str = "high"  # Reasoning effort: low, medium, high, xhigh, max

    # Database
    db_path: str = "data/claude_slack_bot.db"

    # Media
    image_api_url: str = ""
    image_api_key: str = ""

    # Claude Code working directory (where sessions run)
    cwd: str = ""  # e.g. "/home/linke/Projects/my-repo" — defaults to bot's cwd if empty
    projects_dir: str = str(Path.home() / "Projects")  # parent dir to search when user types "cd gr00t"

    # Behaviour
    default_backend: str = "codex"  # "claude-code", "codex", "messages", or "managed"
    codex_bin: str = "codex"
    codex_bypass_approvals_and_sandbox: bool = True
    max_turns_per_thread: int = 200
    summary_interval_turns: int = 5
    confirmation_timeout_seconds: int = 300

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
