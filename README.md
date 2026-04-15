# claude-slack-bot

Threaded Claude agent conversations in Slack. One thread = one task.

Talk to Claude by @mentioning the bot in any channel. Claude responds in a thread, and all follow-up messages in that thread continue the same conversation. Claude can execute code, search the web, generate images and videos, and send files directly in the thread.

## Features

- **Threaded conversations** — each Slack thread is an independent Claude session
- **Full agent capabilities** — bash execution, file operations, web search
- **Media support** — Claude can generate and upload images (matplotlib, PIL) and videos (ffmpeg, moviepy)
- **Permission system** — tool use requires approval via Slack buttons (Allow / Deny / Auto-approve)
- **Auto-approve mode** — toggle per-thread to skip permission prompts
- **Conversation summaries** — Claude automatically appends a summary to each response
- **Three backends** — Claude Code CLI (uses your subscription, no API key), Messages API, or Managed Agents API

## Quick start

```bash
git clone https://github.com/kevin-thankyou-lin/claude-slack-bot.git
cd claude-slack-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your Slack tokens (see Slack App Setup below)
# No API key needed — uses your Claude Code subscription by default
python -m claude_slack_bot.main
```

> **No Anthropic API key?** The default `claude-code` backend uses your existing Claude subscription via the `claude` CLI. Just make sure you've run `claude auth` first.

## Slack App Setup

### 1. Create a Slack App

**Option A — Import manifest (recommended):**
1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From an app manifest**
3. Select your workspace
4. Paste the contents of `manifest.json` from this repo
5. Click **Create** — all scopes, events, and settings are pre-configured
6. Skip to step 6 (Install the App)

**Option B — Manual setup:**
1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From scratch**
3. Name it (e.g., "Claude Bot") and select your workspace
4. Click **Create App**

### 2. Enable Socket Mode

1. Go to **Settings > Socket Mode** in the left sidebar
2. Toggle **Enable Socket Mode** on
3. Create an app-level token:
   - Name: `socket-mode-token`
   - Scope: `connections:write`
   - Click **Generate**
4. Copy the token (`xapp-...`) — this is your `SLACK_APP_TOKEN`

### 3. Configure Bot Token Scopes

1. Go to **Features > OAuth & Permissions**
2. Under **Bot Token Scopes**, add:
   - `app_mentions:read` — detect @mentions
   - `chat:write` — send messages
   - `channels:history` — read channel messages
   - `groups:history` — read private channel messages
   - `im:history` — read DMs
   - `mpim:history` — read group DMs
   - `files:write` — upload files
   - `files:read` — read file metadata
   - `users:read` — read user info

### 4. Subscribe to Events

1. Go to **Features > Event Subscriptions**
2. Toggle **Enable Events** on
3. Under **Subscribe to bot events**, add:
   - `app_mention` — when someone @mentions the bot
   - `message.channels` — messages in public channels
   - `message.groups` — messages in private channels
   - `message.im` — direct messages

### 5. Enable Interactivity

1. Go to **Features > Interactivity & Shortcuts**
2. Toggle **Interactivity** on
3. (No request URL needed — Socket Mode handles this)

### 6. Install the App

1. Go to **Settings > Install App**
2. Click **Install to Workspace** and authorize
3. Copy the **Bot User OAuth Token** (`xoxb-...`) — this is your `SLACK_BOT_TOKEN`

### 7. Invite the Bot to Channels

In Slack, invite the bot to any channel where you want to use it:
```
/invite @Claude Bot
```

## Configuration

Copy `.env.example` to `.env` and fill in:

```bash
SLACK_BOT_TOKEN=xoxb-...          # From step 6
SLACK_APP_TOKEN=xapp-...          # From step 2
# That's it! No API key needed with the default claude-code backend.
```

Optional settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_BACKEND` | `claude-code` | `claude-code` (uses subscription), `messages` (API), or `managed` (beta) |
| `DEFAULT_MODEL` | `sonnet` | `sonnet`, `opus`, `haiku`, or full model ID |
| `DB_PATH` | `data/claude_slack_bot.db` | SQLite database path |
| `SUMMARY_INTERVAL_TURNS` | `5` | Post a summary every N turns |
| `CONFIRMATION_TIMEOUT_SECONDS` | `300` | Auto-expire unanswered permission prompts |
| `LOG_LEVEL` | `INFO` | Logging level |

## Usage

### Start a conversation

@mention the bot in any channel:
```
@Claude Bot help me write a Python script to parse CSV files
```

Claude responds in a thread. All replies in that thread continue the conversation.

### Permission prompts

When Claude wants to execute code or perform actions, it posts a permission request with three buttons:

- **Allow** — approve this single action
- **Deny** — reject this action
- **Auto-approve all** — approve this and all future actions in this thread

### Media generation

Ask Claude to create visuals:
```
@Claude Bot create a bar chart comparing Python, Rust, and Go performance
```

Claude writes and executes a matplotlib script, then uploads the image to the thread.

### Managed Agents (optional)

For stateful sessions with built-in tools (bash, text editor, web search, computer use):

```bash
python -m scripts.create_agent
# Copy the output AGENT_ID and AGENT_VERSION to your .env
# Set DEFAULT_BACKEND=managed
```

## Development

```bash
pip install -e ".[dev]"

# Lint
ruff check src/ tests/
ruff format src/ tests/

# Type check
pyright src/

# Test
pytest

# Run
python -m claude_slack_bot.main
```

## Architecture

```
Slack (Socket Mode)
  → Slack Bolt event router
  → ThreadCoordinator (thread_ts ↔ agent session)
  → AgentBackend (Messages API or Managed Agents)
  → Response → Slack thread
```

- **ThreadCoordinator** maps each Slack thread to an agent session
- **PermissionManager** tracks auto-approve state per thread
- **MessagesBackend** uses the stable Anthropic Messages API with a local agentic loop
- **ManagedAgentBackend** uses the beta Managed Agents API for stateful server-side sessions
- **SQLite** persists thread mappings, message history, and pending confirmations

## License

MIT
