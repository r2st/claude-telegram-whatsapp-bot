# Telechat

[![npm](https://img.shields.io/npm/v/telechat?label=npm)](https://www.npmjs.com/package/telechat)
[![PyPI](https://img.shields.io/pypi/v/telechatai?label=pypi)](https://pypi.org/project/telechatai/)
[![Python](https://img.shields.io/pypi/pyversions/telechatai)](https://pypi.org/project/telechatai/)
[![Tests](https://github.com/telechatai/telechat/actions/workflows/pytest.yml/badge.svg)](https://github.com/telechatai/telechat/actions/workflows/pytest.yml)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

> **Claude AI on your phone / desktop** — personal, self-hosted, zero-infrastructure.  
> Supports **Telegram**, **WhatsApp**, **Slack**, **Discord**, and a **local web chat** simultaneously from a single process.

[telechat.fyi](https://telechat.fyi) · [Blog](https://telechat.fyi/blog/)

A bot that connects to Claude AI via two modes:

- **CLI mode** — Uses the Claude Code CLI (`claude`). No API key needed if you have a Claude subscription.
- **API mode** — Uses the Anthropic API directly. Requires an API key. Works in Docker.

And one thing no other Claude-on-Telegram bot does: a **[Claude Desktop bridge](#claude-desktop-bridge)**. When a Claude Code session on your machine finishes a turn or stalls on a question, you get a triage card on Telegram — status first, the pending decision pulled to the top. Reply to the card and your answer is injected back into that session with `claude --resume`. Optionally, every Bash/Write/Edit call waits for your tap.

## Install

```bash
npm install -g telechat
telechat init
```

That's it. `telechat init` walks you through each platform interactively using Claude CLI — it opens the right pages, grabs your tokens, validates everything, and writes your config.

**Just want to see it work?** Skip the setup entirely:

```bash
telechat web
```

That starts the local web chat on `http://127.0.0.1:8585` — no bot token, no messenger account, nothing written to disk. It only needs a way to reach Claude (the Claude Code CLI, or an `ANTHROPIC_API_KEY`), and it tells you which is missing if neither is there.

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
telechat web          # Local web chat only — no account, no .env needed
telechat doctor       # Diagnose configuration and connectivity
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

| | Telegram | WhatsApp | Slack | Discord | Web chat |
|--|----------|----------|-------|---------|----------|
| Bridge | Telegram Bot API | [Green API](https://green-api.com) free tier | Slack Bolt + Socket Mode | discord.py gateway | Local aiohttp server |
| Setup | Talk to @BotFather | Scan a QR code | Create a Slack app | Create a Discord app | Nothing — open the URL |
| Photo / file support | Yes | Text only | Text only | Text only | Text only |
| Interactive UI | Inline buttons | No | Reactions as status indicator | Typing indicator | Full browser UI |
| Works without public URL | Yes (polling) | Yes (polling) | Yes (WebSocket) | Yes (WebSocket) | It *is* local |
| Works on corporate Wi-Fi | Depends | Yes | Yes | Yes | Yes |

Telegram is the most complete adapter; the others cover the core chat loop. Discord needs the `discord` extra (`pip install 'telechatai[discord]'`). The web chat needs no account anywhere, which makes it the fastest way to try telechat before setting up a messenger.

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

#### Approval timeout policy

Fail-open is the default because the usual case is you sitting at the machine. If you turn approval on precisely *because* you're away from it, invert that:

```bash
BRIDGE_APPROVAL_TIMEOUT=300                 # seconds to wait for a tap (default 300)
BRIDGE_APPROVAL_TIMEOUT_ACTION=fallthrough  # fallthrough (default) | deny | allow
```

- `fallthrough` — hand the decision back to Claude Code's normal permission flow (what it has always done).
- `deny` — refuse the tool call. Claude Code sees a reason naming this setting.
- `allow` — permit it. Only sensible with a short timeout and a trusted machine.

The card itself tells you which one it will do ("Auto-denies in 5 min"), so the policy is visible at the moment you'd act on it. An unrecognised value means `fallthrough` — a typo can't silently become a security posture.

### Uninstall

```bash
telechat bridge uninstall
```

Removes the hook entries from `~/.claude/settings.json`. Bridge tables stay in `bot.db` for reference but no longer fire.

---

## Setup

### 1 — Choose your platform(s)

Set `BOT_MODE` in `.env` — accepts a comma-separated list or a shorthand:

| Value | What starts |
|-------|-------------|
| `telegram` | Telegram only *(default)* |
| `whatsapp` | WhatsApp only |
| `slack` | Slack only |
| `discord` | Discord only |
| `web` | Local web chat only — no messenger account needed |
| `telegram,slack` | Telegram + Slack |
| `telegram,whatsapp` | Telegram + WhatsApp |
| `telegram,web` | Telegram + web chat |
| `both` | Telegram + WhatsApp (legacy alias) |
| `all` | All five |

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

### 2d — Web chat (no account anywhere)

The fastest way to try telechat: a local browser UI with markdown rendering, session switching, and stats. Nothing to register, no messenger involved.

```bash
telechat web              # no .env needed at all
telechat web --port 9000  # if 8585 is taken
```

To make it part of your normal setup instead, put it in `.env`:

```env
BOT_MODE=web
WEB_CHAT_PORT=8585
WEB_CHAT_TOKEN=pick-something-long   # required if you bind beyond loopback
```

Then `telechat` and open http://127.0.0.1:8585.

Bound to loopback it is reachable from that machine only. To open it on your phone over the same network, set `WEB_CHAT_BIND=0.0.0.0` **and** `WEB_CHAT_TOKEN` — then startup prints a scannable QR code for the LAN URL.

It binds `127.0.0.1` by default and **refuses to start exposed without a token** — set `WEB_CHAT_BIND=0.0.0.0` and `WEB_CHAT_TOKEN` together, or it will tell you why it stopped. Behind a reverse proxy, set `WEB_CHAT_TRUST_PROXY=1` so client IPs (used for the auth lockout) come from `X-Forwarded-For` rather than the proxy's own address.

---

### 3 — Configure Claude

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MODE` | `cli` | `cli` or `api` |
| `ANTHROPIC_API_KEY` | — | Required for API mode |
| `CLAUDE_API_MODEL` | current Sonnet | API mode model |
| `MODEL_HAIKU` / `MODEL_SONNET` / `MODEL_OPUS` | current IDs | Model tier overrides (smart routing, planner, memory, judge) |
| `CLAUDE_SYSTEM_PROMPT` | _(generic)_ | Your personal instructions to Claude |
| `CLAUDE_CLI_WORK_DIR` | `~` | Working directory for CLI |
| `CLAUDE_ADD_DIRS` | — | Comma-separated extra dirs Claude can access |
| `CLAUDE_CLI_PERMISSION_MODE` | `auto` | `acceptEdits` / `auto` / `bypassPermissions` |
| `CLAUDE_CLI_MODEL` | `sonnet` | CLI model: `haiku` / `sonnet` / `opus` |
| `CLAUDE_TIMEOUT` | `180` | Seconds to wait for Claude |
| `RATE_LIMIT_REQUESTS` | `20` | Max messages per window |
| `RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |

This is the subset you are most likely to touch. **[`docs/configuration.md`](docs/configuration.md) is the full reference** — every one of the 137 variables the code reads, with its real default, generated from the source so it cannot drift.

> Two of these used to be documented under names the code never read: `SYSTEM_PROMPT` (now `CLAUDE_SYSTEM_PROMPT`) and `CLAUDE_CLI_ADD_DIRS` (now `CLAUDE_ADD_DIRS`). If your `.env` uses the old names they still work — they are read as fallbacks — but the ones above are canonical.

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

CLI mode needs the `claude` binary and its host authentication, so the image runs
in API mode — put `ANTHROPIC_API_KEY` in your `.env` before starting.

```bash
docker compose up -d
docker logs -f telechat
curl http://127.0.0.1:8484/health     # component status, write path, uptime
```

`bot.db` (conversations, memory, cost tracking) lives on the `telechat-data`
volume, so rebuilding the image keeps your history. The container runs as a
non-root user and its health endpoint is published on loopback only.

`/health` returns 200 when healthy and 503 when degraded, which is what the
image's `HEALTHCHECK` uses. Alongside component status it reports the database
write path:

```json
"database": {
  "writer_alive": true,      // false with a live queue → 503: every write is
                             //   going through the synchronous fallback
  "queue_depth": 0,          // early warning; contention shows up here first
  "queue_capacity": 1000,
  "pending_invalidations": 0,
  "retries": 0,              // writes retried after lock/busy contention
  "failures": 0,             // writes given up on permanently — data lost
  "sync_fallbacks": 0        // writes that bypassed the queue, weakening ordering
}
```

`failures` and `sync_fallbacks` are counters, not states: they describe
something that already happened, so they are reported but do not by themselves
fail the check.

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
│   ├── dev-setup.sh         One command to a working dev environment
│   ├── env_reference.py     Generates docs/configuration.md from the source
│   └── publish.sh           PyPI + npm release script
├── npm/bin/telechat.js      CLI entry point
├── docs/configuration.md    Every environment variable, generated
├── Dockerfile               (API mode only)
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## How this project is built

Most of the work here is done by AI agents coordinating through a file-based
ticket system, and that record is public:

- **`agents/inbox/`** — unclaimed tickets, i.e. what happens next.
  **`agents/tasks/`** — claimed. **`agents/done/`** — shipped, each with an
  outcome summary and test evidence.
- Every ticket declares a `touches:` list of the files it will modify, and
  `agents/check-overlap.sh <NNNN>` refuses a claim that collides with an active
  one. That is what lets several agents work the same tree at once.
- **`docs/decisions/`** holds ADRs for anything someone might later
  second-guess; **`docs/improvements.md`** is the current standing review —
  what is broken, what is worth doing, in priority order, with the fixed items
  annotated in place.

The guardrails are mechanical rather than honour-system: CI runs the full
pytest suite across Python 3.10–3.13 with a coverage floor, `ruff`, a
version-consistency check, a Docker build, and a check that
`docs/configuration.md` still matches the code.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Security
issues go through [SECURITY.md](SECURITY.md), which also states the trust
boundary plainly: **this is a single-operator tool, and in CLI mode the bot runs
with your Claude authentication and your filesystem access.** Read that before
adding a second person to the allowlist.

---

## Features

- **[Claude Desktop bridge](#claude-desktop-bridge)** — Claude Code sessions page you on Telegram with an AI triage card; reply to resume them, and gate tool calls on your approval
- **Four surfaces, one process** — Telegram, WhatsApp, Slack, and a local web chat sharing one set of sessions, memories and history (`BOT_MODE=all`)
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

**[SECURITY.md](SECURITY.md) states the trust boundary in full**, and it is
worth two minutes: access control is a flat allowlist, so everyone on it shares
one Claude authentication, one working directory, and one permission ceiling
that any of them can raise. That is fine for you and your phone. It is not a
multi-tenant deployment. It also covers how to report a vulnerability — please
do that privately rather than in a public issue.

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
