"""Tests for auto-poll extraction: ETA detection + interval picking + promise fallback."""

from __future__ import annotations

import pytest

from claude_slack_bot.core.coordinator import (
    AUTO_POLL_INTERVAL_MIN,
    ONGOING_POLL_INTERVAL_MIN,
    ThreadCoordinator,
    _find_earliest_eta,
    _parse_eta_minutes,
    _pick_poll_interval,
    _StreamBuffer,
)


class _FakeCoord:
    """Stand-in exposing just the `_extract_poll_request` method."""

    def __init__(self) -> None:
        # Grab the unbound method — we only need _extract_poll_request which reads buf._text
        self._fn = ThreadCoordinator._extract_poll_request

    def extract(self, text: str) -> tuple[int, str, str] | None:
        buf = _StreamBuffer("t1", None, None)
        buf._text = text
        return self._fn(self, buf)


# ── _parse_eta_minutes ────────────────────────────────────────────────────────


def test_parse_eta_min_single() -> None:
    assert _parse_eta_minutes("15", None, "min") == 15
    assert _parse_eta_minutes("15", None, "minutes") == 15
    assert _parse_eta_minutes("15", None, "m") == 15


def test_parse_eta_min_range_uses_upper() -> None:
    assert _parse_eta_minutes("10", "20", "min") == 20


def test_parse_eta_hours() -> None:
    assert _parse_eta_minutes("2", None, "hours") == 120
    assert _parse_eta_minutes("1", None, "hr") == 60
    assert _parse_eta_minutes("3", None, "h") == 180


def test_parse_eta_seconds_rounds_up() -> None:
    assert _parse_eta_minutes("30", None, "seconds") == 1
    assert _parse_eta_minutes("120", None, "sec") == 2
    assert _parse_eta_minutes("90", None, "s") == 2


# ── _pick_poll_interval ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("eta_min", "expected"),
    [
        (1, 2),  # very short → 2m
        (5, 2),
        (6, 3),  # just above 5m threshold
        (15, 3),  # 15/4 = 3
        (20, 5),  # 20/4 = 5
        (30, 5),  # clamped to 5
        (45, 10),  # 45/6 = 7 → clamped up to 10
        (60, 10),
        (90, 15),  # 90/6 = 15
        (120, 15),  # clamped
        (180, 22),  # 180/8 = 22
        (300, 30),  # clamped
    ],
)
def test_pick_poll_interval(eta_min: int, expected: int) -> None:
    assert _pick_poll_interval(eta_min) == expected


# ── _find_earliest_eta ────────────────────────────────────────────────────────


def test_find_eta_in_minute_range() -> None:
    text = "First ckpt-25k JSONs expected in ~10-20 min."
    hit = _find_earliest_eta(text)
    assert hit is not None
    eta, _, subject = hit
    assert eta == 20
    assert "JSONs" in subject or "ckpt" in subject.lower()


def test_find_eta_in_single_min() -> None:
    text = "first JSONs in ~15 min"
    hit = _find_earliest_eta(text)
    assert hit is not None
    assert hit[0] == 15


def test_find_eta_ckpt_in_30_min() -> None:
    text = "ckpt in ~30 min"
    hit = _find_earliest_eta(text)
    assert hit is not None
    assert hit[0] == 30


def test_find_eta_eta_prefix() -> None:
    text = "ETA ~5 min"
    hit = _find_earliest_eta(text)
    assert hit is not None
    assert hit[0] == 5


def test_find_eta_about_hours() -> None:
    text = "ready in about 2 hours"
    hit = _find_earliest_eta(text)
    assert hit is not None
    assert hit[0] == 120


def test_find_eta_picks_earliest() -> None:
    text = "first JSONs in ~15 min, full results in ~1 hour"
    hit = _find_earliest_eta(text)
    assert hit is not None
    assert hit[0] == 15


def test_find_eta_returns_none_when_missing() -> None:
    assert _find_earliest_eta("all done — everything completed") is None
    assert _find_earliest_eta("") is None


def test_find_eta_ignores_past_tense_25_min_in() -> None:
    # "~25 min in" means 25 minutes INTO the run (past), not an ETA
    text = "early in run, ~25 min in. No JSONs yet."
    hit = _find_earliest_eta(text)
    # It's OK if this matches — we just don't want it to match with a wild number.
    # The word "in" comes AFTER the number here, so our regex should skip it.
    assert hit is None


# ── _extract_poll_request (integration) ───────────────────────────────────────


def test_extract_explicit_poll_start_wins() -> None:
    text = "POLL_START: 10m check log\nFirst JSONs in ~15 min"
    result = _FakeCoord().extract(text)
    assert result == (10, "m", "check log")


def test_extract_auto_poll_from_eta() -> None:
    text = "D4 evals all alive on GPUs 1/2/3/7 — no JSONs yet. First ckpt-25k JSONs expected in ~10-20 min."
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, unit, prompt = result
    assert unit == "m"
    # ETA = 20 min (upper bound of range) → interval = 5 min (20/4 clamped)
    assert amount == 5
    assert "20 minutes" in prompt or "~20" in prompt
    assert "POLL_COMPLETE" in prompt


def test_extract_auto_poll_from_eta_15_min() -> None:
    text = "first JSONs in ~15 min"
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, unit, _ = result
    assert (amount, unit) == (3, "m")  # 15/4 = 3


def test_extract_auto_poll_from_eta_30_min() -> None:
    text = "ckpt in ~30 min — will be ready then"
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, _, _ = result
    # 30 min → 30/4=7, clamped to max 5 → 5
    assert amount == 5


def test_extract_auto_poll_from_eta_2_hours() -> None:
    text = "build should finish in about 2 hours"
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, _, _ = result
    # 120 min → 120/6=20, clamped to 15 → 15
    assert amount == 15


def test_extract_falls_back_to_promise_when_no_eta() -> None:
    text = "Sent. Will report back when supervisor answers."
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, unit, prompt = result
    assert (amount, unit) == (AUTO_POLL_INTERVAL_MIN, "m")
    assert "follow up" in prompt.lower()


def test_extract_returns_none_on_plain_answer() -> None:
    text = "The answer is 42. All tasks completed."
    result = _FakeCoord().extract(text)
    assert result is None


# ── ongoing-work fallback (no ETA, no explicit promise) ───────────────────────


def test_extract_ongoing_eval_running() -> None:
    text = (
        "D4 evals all alive on GPUs 1/2/3/7 (16 GB each, GPU 7 just started rolling out). "
        "No JSONs yet — early in the run."
    )
    # ETA-less but clearly still in progress: should fall back to ongoing
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, unit, prompt = result
    assert (amount, unit) == (ONGOING_POLL_INTERVAL_MIN, "m")
    assert "POLL_COMPLETE" in prompt


def test_extract_ongoing_training_in_progress() -> None:
    text = "Training in progress, step 5000/50000."
    result = _FakeCoord().extract(text)
    assert result is not None
    assert result[0] == ONGOING_POLL_INTERVAL_MIN


def test_extract_ongoing_setting_up_envs() -> None:
    text = "Workers are setting up envs. No checkpoints yet."
    result = _FakeCoord().extract(text)
    assert result is not None
    assert result[0] == ONGOING_POLL_INTERVAL_MIN


def test_extract_ongoing_does_not_trigger_when_done() -> None:
    text = "All evals completed successfully with success_rate=0.9. Done."
    result = _FakeCoord().extract(text)
    assert result is None


def test_extract_eta_wins_over_ongoing() -> None:
    # Both ongoing signals AND an ETA — ETA wins (more specific interval)
    text = "D4 evals all alive on GPUs 1/2/3/7 — setting up envs. First ckpt-25k JSONs expected in ~10-20 min."
    result = _FakeCoord().extract(text)
    assert result is not None
    amount, _, prompt = result
    # ETA branch: 20 min → interval 5 min
    assert amount == 5
    # Prompt should reference the predicted ETA
    assert "~20 minutes" in prompt or "~20" in prompt
