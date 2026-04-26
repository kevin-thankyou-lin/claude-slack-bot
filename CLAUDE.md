# claude-slack-bot

Threaded Claude agent conversations in Slack. One thread = one task.

## Project layout

- `src/claude_slack_bot/` — main package
  - `slack/` — Slack Bolt app, event listeners, Block Kit, file uploads
  - `agent/` — Anthropic backend (Messages API + Managed Agents), tools, prompts
  - `core/` — thread coordinator, permission manager, media pipeline
  - `db/` — SQLite persistence (aiosqlite)
  - `utils/` — rate limiting, structured logging
- `tests/` — pytest-asyncio tests
- `scripts/` — one-time setup scripts

## Dev commands

```bash
pip install -e ".[dev]"
ruff check src/ tests/
ruff format src/ tests/
pyright src/
pytest
python -m claude_slack_bot.main   # run the bot
```

## Architecture

Slack Bolt (Socket Mode) → ThreadCoordinator → AgentBackend (Messages API or Managed Agents) → responses back to Slack thread.

Thread-to-session mapping persisted in SQLite. Permission requests rendered as Block Kit buttons. Auto-approve mode toggleable per thread.

## Slack file upload

Use the modern 3-step flow (`files.getUploadURLExternal` → `POST` to upload_url → `files.completeUploadExternal`). This is already implemented in `slack/file_upload.py`.

**Known scope gaps:** The bot token has `files:write` but NOT `channels:read`, so `conversations.list` returns `missing_scope`. You cannot look up a channel ID by name via the API — use the known channel ID from bot logs or hardcode it.

**Auto-upload for Claude Code backend:** `scan_and_upload_files` scans `/tmp/` paths in tool output, but the Claude Code backend runs tools in-process — only TEXT events reach the coordinator, so the scan was never triggered. Fixed in `core/coordinator.py`: `scan_and_upload_files` is now also called on `final_text` after `buf.finalize()` at the end of each turn. This means any `/tmp/*.png|jpg|mp4|...` path mentioned in an assistant response will be auto-uploaded to the thread.

**DO NOT manually upload files from within the bot agent.** The coordinator's `scan_and_upload_files` already handles this automatically — just save the file to `/tmp/` and mention its path in your response. The bot will upload it to the correct thread.

Manual uploads are only needed for one-off developer scripts run *outside* the bot. If you must do one manually as a developer, look up the current channel_id and thread_ts from bot logs (`coordinator.new_thread channel=...`) — never use hardcoded values.
