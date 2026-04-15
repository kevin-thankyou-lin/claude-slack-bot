from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from claude_slack_bot.config import Settings
from claude_slack_bot.db.database import Database


@pytest.fixture
def settings() -> Settings:
    return Settings(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        anthropic_api_key="sk-ant-test",
        db_path=":memory:",
        default_backend="messages",
        default_model="claude-sonnet-4-6",
    )


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> Database:
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    return db


@pytest.fixture
def mock_say() -> AsyncMock:
    say = AsyncMock()
    say.return_value = {"ts": "1234567890.123456", "channel": "C123"}
    return say


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.files_getUploadURLExternal.return_value = {
        "upload_url": "https://files.slack.com/upload/test",
        "file_id": "F123",
    }
    client.files_completeUploadExternal.return_value = {"files": [{"id": "F123"}]}
    return client
