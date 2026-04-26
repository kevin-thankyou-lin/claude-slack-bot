from __future__ import annotations

import time

# Shared watchdog state. Lives in its own module so it is the same object whether
# imported as `claude_slack_bot.utils.watchdog` from package code or from the
# `__main__` entrypoint (`python -m claude_slack_bot.main`).
_last_event_time: float = time.monotonic()


def touch() -> None:
    global _last_event_time  # noqa: PLW0603
    _last_event_time = time.monotonic()


def idle_seconds() -> float:
    return time.monotonic() - _last_event_time
