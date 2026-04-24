from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

UTC = timezone.utc


@dataclass
class Thread:
    thread_ts: str
    channel_id: str
    session_id: str
    backend_type: str = "messages"
    auto_approve: bool = False
    cwd: str = ""
    cc_session_id: str = ""
    model: str = ""
    effort: str = ""
    status: str = "active"
    user_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Message:
    id: int
    thread_ts: str
    role: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    slack_msg_ts: str | None = None


@dataclass
class PendingConfirmation:
    tool_use_id: str
    thread_ts: str
    tool_name: str
    tool_input: str  # JSON string
    status: str = "pending"
    slack_msg_ts: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
