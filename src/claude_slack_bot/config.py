from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    agent_id: str = ""
    agent_version: int = 1
    default_model: str = "sonnet"  # "sonnet", "opus", "haiku", or full model ID

    # Database
    db_path: str = "data/claude_slack_bot.db"

    # Media
    image_api_url: str = ""
    image_api_key: str = ""

    # Behaviour
    default_backend: str = "claude-code"  # "claude-code", "messages", or "managed"
    max_turns_per_thread: int = 200
    summary_interval_turns: int = 5
    confirmation_timeout_seconds: int = 300

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
