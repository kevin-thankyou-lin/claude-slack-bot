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
