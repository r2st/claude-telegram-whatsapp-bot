# Telechat

> **Claude AI on your phone / desktop** — personal, self-hosted, zero-infrastructure.  
> Supports **WhatsApp**, **Telegram**, and **Slack** simultaneously from a single process.

A bot that connects to Claude AI via two modes:

- **CLI mode** — Uses the Claude Code CLI (`claude`). No API key needed if you have a Claude subscription.
- **API mode** — Uses the Anthropic API directly. Requires an API key. Works in Docker.

## Install

```bash
npm install -g telechat
telechat init
```

That's it. `telechat init` walks you through each platform interactively using Claude CLI — it opens the right pages, grabs your tokens, validates everything, and writes your config.

Requires: [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code && claude auth login`)

### Alternative installs

```bash
# pip
pip install telechatai
telechat init

# npx (no global install)
npx telechat init

# From source
git clone https://github.com/telechatai/telechat.git
cd telechat && pip install -e .
telechat init
```

### Manual setup (no Claude CLI)

```bash
telechat setup
```

Step-by-step wizard with prompts. Works without Claude CLI.

## Commands

```bash
telechat              # Start bot as background service
telechat stop         # Stop the bot
telechat restart      # Restart the bot
telechat status       # Check if running
telechat logs         # Tail the bot log
telechat env          # Show environment variables (tokens masked)
telechat env clean    # Remove .env file (clear all credentials)
telechat clean        # Same as env clean
telechat init         # AI-guided setup using Claude CLI
telechat setup        # Manual setup wizard
telechat update       # Update to latest version
telechat --debug      # Start with verbose logging
telechat --version    # Show version
telechat --help       # Show all commands
```

The bot runs as a **background service** — it survives terminal close and Ctrl+C. Manage it with `start/stop/restart/status`.

---

## Platform comparison

| | Telegram | WhatsApp | Slack |
|--|----------|----------|-------|
| Bridge | Telegram Bot API | [Green API](https://green-api.com) free tier | Slack Bolt + Socket Mode |
| Setup | Talk to @BotFather | Scan a QR code | Create a Slack app |
| Photo / file support | Yes | Text only | Text only |
| Interactive UI | Inline buttons | No | Reactions as status indicator |
| Works without public URL | Yes (polling) | Yes (polling) | Yes (WebSocket) |
| Works on corporate Wi-Fi | Depends | Yes | Yes |

---

## Setup

### 1 — Choose your platform(s)

Set `BOT_MODE` in `.env` — accepts a comma-separated list or a shorthand:

| Value | What starts |
|-------|-------------|
| `telegram` | Telegram only *(default)* |
| `whatsapp` | WhatsApp only |
| `slack` | Slack only |
| `telegram,slack` | Telegram + Slack |
| `telegram,whatsapp` | Telegram + WhatsApp |
| `both` | Telegram + WhatsApp (legacy alias) |
| `all` | All three platforms |

---

### 2a — Telegram setup

1. Open **Telegram Web**: https://web.telegram.org/k/
2. Log in by scanning the QR code:
   - Open Telegram on your phone
   - Go to **Settings → Devices → Link Desktop Device**
   - Point your phone camera at the QR code
3. Search for **@BotFather** and send `/newbot`
4. Pick a display name and a username (must end in `bot`)
5. Copy the token → set `TELEGRAM_BOT_TOKEN` in `.env`

**Finding your user ID (for access control)**

1. Search for **@userinfobot** in Telegram Web
2. Send any message — it replies with your numeric ID
3. Copy the ID → set `TELEGRAM_ALLOWED_USER_IDS` in `.env`

**Optional: customize your bot**

| BotFather command | What it does |
|-------------------|--------------|
| `/setdescription` | Text users see before starting the bot |
| `/setabouttext` | Bio shown on the bot's profile |
| `/setuserpic` | Profile picture |
| `/setcommands` | Register autocomplete hints |

Register command hints:
```
start - Welcome message
reset - Clear conversation history
mode - Show current mode and model
id - Show your Telegram user ID
```

---

### 2b — Slack setup (Socket Mode — no public URL needed)

> **Corporate workspace?** Most company Slack workspaces block individual users from installing apps. Create a **free personal workspace** at [slack.com/get-started](https://slack.com/get-started) instead.

#### Step-by-step

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
   - Enter a name (e.g. "TeleChat") and select your workspace

2. **Socket Mode** → toggle ON
   - Create an App-Level Token: name it `telechat`, add scope `connections:write`
   - Copy the `xapp-...` token → this is `SLACK_APP_TOKEN`

3. **OAuth & Permissions** → scroll to **Bot Token Scopes** → add:

   | Scope | Purpose |
   |-------|---------|
   | `chat:write` | Send messages |
   | `channels:history` | Read public channel messages |
   | `groups:history` | Read private channel messages |
   | `im:history` | Read DMs |
   | `im:write` | Open DM conversations |
   | `app_mentions:read` | Detect @mentions |
   | `reactions:write` | Show ⏳ while Claude thinks |

4. **Event Subscriptions** → toggle ON → Subscribe to bot events:
   `message.im`, `message.channels`, `message.groups`, `app_mention` → **Save Changes**

5. **Install App** → **Install to Workspace** → Allow
   - Copy the **Bot User OAuth Token** (`xoxb-...`) → this is `SLACK_BOT_TOKEN`
   - ⚠ **NOT** the User OAuth Token (`xoxp-`/`xoxe-`) — that won't work

6. Find your Slack member ID: click your profile pic → **Profile** → **⋮** → **Copy member ID**

#### `.env` for Slack

```env
BOT_MODE=slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U01234567
```

#### How it works

| Trigger | How to use |
|---------|-----------|
| Direct message | Just message the bot |
| Channel | `@yourbot <question>` |
| Thread | Reply mentioning the bot to keep conversation in-thread |

A ⏳ reaction appears on your message while Claude is thinking, removed when done.

---

### 2c — WhatsApp setup (Green API — free, no Meta account needed)

1. Sign up at https://console.green-api.com (free Developer plan)
2. You'll see a free instance on the dashboard
3. Find **idInstance** (a number) and **apiTokenInstance** (a long hex string) at the top of your instance
4. Link your WhatsApp phone:
   - Click your instance → look for the QR code section
   - On your phone: **WhatsApp → Settings → Linked Devices → Link a Device**
   - Scan the QR code with your phone camera
5. Copy credentials into `.env`:

```env
BOT_MODE=whatsapp
GREEN_API_INSTANCE_ID=1234567890
GREEN_API_TOKEN=your_token_here
WHATSAPP_ALLOWED_NUMBERS=919876543210   # your number without the +
```

> **Corporate network note:** Green API works over standard HTTPS polling — no webhook or public URL needed.

---

### 3 — Configure Claude

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODE` | `cli` | `cli` or `api` |
| `ANTHROPIC_API_KEY` | — | Required for API mode |
| `CLAUDE_MODEL` | `claude-sonnet-5` | API mode model |
| `MODEL_HAIKU` / `MODEL_SONNET` / `MODEL_OPUS` | current IDs | Model tier overrides (smart routing, planner, memory, judge) |
| `SYSTEM_PROMPT` | _(generic)_ | Your personal instructions to Claude |
| `CLAUDE_CLI_WORK_DIR` | `~` | Working directory for CLI |
| `CLAUDE_CLI_ADD_DIRS` | — | Comma-separated extra dirs Claude can access |
| `CLAUDE_CLI_PERMISSION_MODE` | — | `acceptEdits` / `auto` / `bypassPermissions` |
| `CLAUDE_CLI_MODEL` | `sonnet` | CLI model: `haiku` / `sonnet` / `opus` |
| `CLAUDE_TIMEOUT` | `180` | Seconds to wait for Claude |
| `RATE_LIMIT_REQUESTS` | `20` | Max messages per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |

**CLI mode** — requires Claude Code CLI installed and authenticated:

```bash
npm install -g @anthropic-ai/claude-code
claude auth login
```

**API mode:**

```env
CLAUDE_MODE=api
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Running

The bot runs as a background service by default:

```bash
telechat              # Start
telechat stop         # Stop
telechat restart      # Restart
telechat status       # Check status
telechat logs         # Tail logs
telechat --debug      # Start with verbose logging
```

### From source

```bash
./scripts/start.sh                  # Foreground
./scripts/service.sh install        # macOS launchd / Linux systemd
```

### Docker (API mode only)

```bash
docker compose up -d
docker logs -f telechat
```

---

## Telegram commands

**Core**
| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/reset` | Clear conversation history |
| `/mode` | Show current mode and model |
| `/model` | Switch model (haiku / sonnet / opus) |
| `/engine` | Switch between CLI and API mode |
| `/settings` | View all current settings |
| `/verbose` | Set output verbosity |
| `/permissions` | Change CLI permission mode |
| `/usage` | Show usage statistics |
| `/budget` | Set daily/monthly cost limits |
| `/id` | Show your Telegram user ID |

**Sessions**
| Command | Description |
|---------|-------------|
| `/sessions` | List all sessions |
| `/new` | Create a new session |
| `/switch` | Switch to another session |
| `/rename` | Rename a session |
| `/pin` | Pin/unpin a session |
| `/archive` | Archive a session |
| `/resume` | Resume a Claude CLI session |
| `/fork` | Fork current session into a new one |

**Memory**
| Command | Description |
|---------|-------------|
| `/remember` | Save a memory |
| `/recall` | Search memories |
| `/memories` | List all memories |
| `/forget` | Delete a memory |
| `/editmem` | Edit a memory |
| `/exportmem` | Export memories as JSON |
| `/importmem` | Import memories from JSON |

**Tools**
| Command | Description |
|---------|-------------|
| `/code` | Start a coding task |
| `/project` | Set working project directory |
| `/plan` | Multi-step planning agent |
| `/search` | Web search |
| `/fetch` | Fetch and summarize a URL |
| `/web` | Browse a webpage |
| `/kb` | Knowledge base (upload/search docs) |
| `/imagine` | Generate an image |
| `/tts` | Text-to-speech |
| `/music` | Generate music |
| `/video` | Generate video |
| `/poll` | Create a poll |
| `/schedule` | Schedule a task |

---

## WhatsApp usage

Just send a message. There are no slash commands — WhatsApp is intentionally kept simple.

---

## Claude Desktop bridge

Telegram notifications + remote control for your locally-running Claude Desktop sessions. When a session ends a turn or needs input, you get a rich card on your phone. Reply to it (or pick a session from the list) and your message is injected as the next turn via `claude --resume`. Optionally require Telegram approval for every Bash/Write/Edit tool call.

### One-command install

```bash
telechat bridge install                 # hooks + persistent service + preflight checks
telechat bridge install --approval      # also gate Bash/Write/Edit on Telegram approval
telechat bridge install --no-service    # hooks only, skip the launchd service
```

`telechat bridge install` does everything in one shot:

1. **Registers Claude Code hooks** in `~/.claude/settings.json` (Stop, Notification, SubagentStop, and — with `--approval` — PreToolUse)
2. **Installs a persistent background service** (macOS launchd) so the bot auto-starts at login and restarts on crash
3. **Migrates** any older standalone `~/.claude-bridge/` install (and copies its OAuth token)
4. **Runs preflight checks** — Claude CLI present, Telegram credentials set, and a long-lived OAuth token

> **OAuth token:** headless `claude --resume` needs a long-lived token. Create one with `claude setup-token`, then add it to `~/.telechat/.env` as `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...` (the `telechat init` wizard collects it for you). Without it, replies fail with a 401 — and the install's preflight will warn you.

### Service management

```bash
telechat bridge service status      # is the persistent service running?
telechat bridge service install     # (re)install + start it
telechat bridge service uninstall   # stop and remove it
```

### Telegram commands

| Command | Description |
|---|---|
| `/desktop` | List running Claude Desktop sessions with tap-to-select buttons |
| `/desktop_use <id>` | Switch to a session by 8-char short id |
| `/desktop_which` | Show the current session |
| `/desktop_clear` | Clear the current session selection |
| `/desktop_all <msg>` | Broadcast a message to every running session at once |
| `/desktop_approve_on` *(as reply)* | Require Telegram approval for Bash/Write/Edit in that project |
| `/desktop_approve_off` *(as reply)* | Disable approval mode |
| Reply to any session card | Sends your message to that specific session |
| Plain text (after picking a session) | Goes to the current session — no Reply needed |

### AI triage digests

Every notification and session reply is run through a fast model (haiku) that produces a glanceable triage card instead of a wall of text:

```
💬 Reply from apprend-backend [b2ca0347]

⚠️ NEEDS DECISION
Migrated password hashing to argon2id, consolidated token paths,
added refresh rotation. All 31 tests pass, staging verified.

⚠️ NEEDS YOU: Migrate OAuth1 to the new flow or drop legacy support?

[📄 Full output]  [💬 Use session]
```

- **Status at a glance** — ✅ DONE / ⚠️ NEEDS DECISION / ❌ BLOCKED / ℹ️ UPDATE
- **Decisions surface to the top** — if Claude is asking you something, it's pulled out and flagged, so you can decide from your phone
- **Full output on demand** — tap **📄 Full output** to get the complete, untrimmed text (smart-chunked, or attached as `.txt` if huge)

The digest never loses information — the raw output is always one tap away. If summarization is unavailable, the bridge falls back to posting the full chunked text.

### How it works

`telechat bridge install` writes hook entries into `~/.claude/settings.json`:

- `Stop`, `Notification`, `SubagentStop` → `telechat bridge notify <event>` (posts a rich card with the last assistant message snippet)
- `PreToolUse` (with `--approval`) → `telechat bridge approve` (blocks, sends ⚠️ card with Approve/Deny buttons, returns the decision to Claude Code)

Telechat's running Telegram poller dispatches your replies and button taps to the same bridge module — no separate daemon, no second bot needed.

### Limits

- `claude --resume` works best when the target Desktop session is **idle**. Don't reply while Claude is mid-turn on that session — undefined behavior.
- Approval hook times out after 5 minutes and falls through to the normal permission flow, so you won't hang forever if your phone's offline.
- Approval is **off by default** per project — opt in via `/desktop_approve_on` as a reply.

### Uninstall

```bash
telechat bridge uninstall
```

Removes the hook entries from `~/.claude/settings.json`. Bridge tables stay in `bot.db` for reference but no longer fire.

---

## Project structure

```
├── telechat_pkg/
│   ├── main.py              Entry point — reads BOT_MODE, starts adapters
│   ├── claude_core.py       Claude CLI/API invocation layer
│   ├── store.py             SQLite persistence, sessions, history
│   ├── telegram_bot.py      Telegram adapter
│   ├── whatsapp_bot.py      WhatsApp adapter (Green API polling)
│   ├── slack_bot.py         Slack adapter (Socket Mode)
│   ├── memory.py            Per-user memory with FTS5 search
│   ├── session_manager.py   Multi-session conversation management
│   ├── knowledge_base.py    Document store with chunking and search
│   ├── cost_budget.py       Usage tracking and budget alerts
│   ├── coder.py             Chat-based coding agent (/code, /project)
│   ├── two_agent.py         Multi-step planning agent
│   ├── smart_router.py      Model routing by query complexity
│   ├── health.py            Health checks and circuit breaker
│   ├── web_fetch.py         URL content extraction (Jina / raw)
│   ├── link_understanding.py  Auto-detect and fetch URLs in messages
│   ├── tts.py               Text-to-speech via OpenAI
│   ├── image_gen.py         Image generation
│   ├── music_gen.py         Music generation
│   ├── video_gen.py         Video generation
│   └── ...
├── scripts/
│   ├── watchdog.py          Auto-restart and self-healing
│   └── publish.sh           PyPI + npm release script
├── npm/bin/telechat.js      CLI entry point
├── Dockerfile               (API mode only)
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Features

- **Three platforms** — Telegram, WhatsApp, and Slack from one process (`BOT_MODE=all`)
- **One-command setup** — `npm install -g telechat && telechat init`
- **Background service** — runs detached, survives terminal close
- **AI-guided setup** — `telechat init` uses Claude CLI for interactive configuration
- **Dual Claude mode** — CLI (free with Claude subscription) or API
- **Coding agent** — `/code` and `/project` for end-to-end development tasks
- **Memory system** — per-user memories with FTS5 search, remembered across sessions
- **Multi-session conversations** — create, switch, pin, archive named sessions
- **Knowledge base** — upload documents, search with full-text and semantic matching
- **Two-agent planning** — multi-step task execution with progress updates
- **Smart model routing** — auto-selects haiku / sonnet / opus by query complexity
- **Cost tracking & budgets** — daily/monthly limits with alerts
- **Web fetch & link understanding** — auto-extracts content from URLs in messages
- **Media generation** — TTS, image, music, and video generation
- **Health monitoring** — HTTP health endpoint, circuit breakers, auto-recovery watchdog
- **Image & file analysis** — Telegram photos + documents
- **Typing indicator** — shows "typing…" while Claude processes
- **Model switching** — haiku / sonnet / opus from Telegram inline buttons
- **Rate limiting** — configurable per-user throttling
- **Persistent history** — SQLite with WAL mode, async writes, history caching
- **Markdown rendering** — formatted responses with plain-text fallback

---

## Security

- Set `TELEGRAM_ALLOWED_USER_IDS`, `WHATSAPP_ALLOWED_NUMBERS`, or `SLACK_ALLOWED_USER_IDS` to restrict access
- View credentials with `telechat env` (tokens are masked)
- Clear all credentials with `telechat clean`
- Never commit `.env` — it is in `.gitignore`
- In CLI mode the bot inherits your Claude auth — don't run on untrusted machines

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `telechat: command not found` | Run `npm install -g telechat` or use `npx telechat` |
| Bot not responding | Check `telechat status` and `telechat logs` |
| Telegram 409 conflict | Another instance is running — `telechat stop` then `telechat start` |
| WhatsApp: no replies | Check instance status in Green API console — must be `authorized` |
| Slack: "error creating request" | Corporate workspace blocks installs — use a free personal workspace |
| Slack: bot doesn't respond | Check Socket Mode is enabled; App-Level Token needs `connections:write` |
| Slack: wrong token type | Use **Bot User OAuth Token** (`xoxb-...`), not User OAuth Token (`xoxp-`/`xoxe-`) |
| Slack: works in channels not DMs | Add `im:history` + `im:write` scopes and reinstall to workspace |
| `claude: command not found` | Install Claude Code CLI: `npm i -g @anthropic-ai/claude-code` |
| Response cut off | Bot auto-chunks at 4 000 chars per message — expected |
| Bot stops after reboot | Use `./scripts/service.sh install` for a system service |

---

## License

MIT
