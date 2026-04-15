from __future__ import annotations

from claude_slack_bot.slack.blocks import build_permission_block, build_summary_block


def test_permission_block_structure() -> None:
    blocks = build_permission_block("bash", {"command": "ls -la"}, "tool-abc")
    assert len(blocks) == 3
    # Section with tool name
    assert blocks[0]["type"] == "section"
    assert "bash" in blocks[0]["text"]["text"]
    # Section with input preview
    assert blocks[1]["type"] == "section"
    assert "ls -la" in blocks[1]["text"]["text"]
    # Actions with 3 buttons
    assert blocks[2]["type"] == "actions"
    assert len(blocks[2]["elements"]) == 3
    # Check button action IDs
    action_ids = [b["action_id"] for b in blocks[2]["elements"]]
    assert action_ids == ["tool_allow", "tool_deny", "tool_auto_approve"]
    # Check value propagation
    assert all(b["value"] == "tool-abc" for b in blocks[2]["elements"])


def test_permission_block_truncates_long_input() -> None:
    long_input = {"command": "x" * 2000}
    blocks = build_permission_block("bash", long_input, "tool-def")
    input_text = blocks[1]["text"]["text"]
    assert len(input_text) < 1100  # 1000 + some wrapping


def test_summary_block() -> None:
    blocks = build_summary_block("Task completed successfully.", "completed")
    assert len(blocks) == 2
    assert blocks[0]["type"] == "divider"
    assert ":white_check_mark:" in blocks[1]["text"]["text"]
    assert "Task completed successfully." in blocks[1]["text"]["text"]


def test_summary_block_in_progress() -> None:
    blocks = build_summary_block("Still working...", "in_progress")
    assert ":hourglass_flowing_sand:" in blocks[1]["text"]["text"]
