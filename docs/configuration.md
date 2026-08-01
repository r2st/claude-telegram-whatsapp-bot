# Configuration reference

Every environment variable telechat reads, with the default the code
actually uses. Set them in `$TELECHAT_HOME/.env` (`~/.telechat/.env` by
default) or in the process environment.

**This file is generated.** Run `python scripts/env_reference.py` after
adding a variable; `tests/test_config_docs.py` fails if it is stale, and
also fails if a new variable has no description in
`scripts/env_reference.py`. That is deliberate: the reference cannot
silently fall behind the code again.

A blank Default column means the variable is unset by default.

## Core

Which platforms run, and how Claude is reached.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` |  | API key, required when CLAUDE_MODE=api. |
| `BOT_MODE` | `telegram` | Which adapters to run: `telegram`, `whatsapp`, `slack`, `web`, a comma-separated list, or the aliases `both` / `all`. |
| `CLAUDE_ADD_DIRS` |  | Extra directories to grant the CLI access to (comma-separated), passed as `--add-dir`. |
| `CLAUDE_API_MODEL` |  | Model for API mode. Defaults to the current Sonnet in `models.py`. |
| `CLAUDE_CLI_ADD_DIRS` |  | Legacy name for CLAUDE_ADD_DIRS, read as a fallback for the same reason. |
| `CLAUDE_CLI_MODEL` |  | Per-adapter override of the CLI model. Falls back to CLAUDE_MODEL. |
| `CLAUDE_CLI_PERMISSION_MODE` | `auto` | Permission mode handed to the CLI/SDK: `auto` (mapped to Claude Code's `default`), `acceptEdits`, `plan`, or `bypassPermissions`. |
| `CLAUDE_CLI_WORK_DIR` |  | Working directory `claude` runs in. Everything the bot can read or write is relative to this. |
| `CLAUDE_CODE_OAUTH_TOKEN` |  | OAuth token for the Claude CLI, passed through to spawned `claude` processes. |
| `CLAUDE_MODE` | `cli` | `cli` (free with a Claude subscription, uses the `claude` binary) or `api` (billed, uses ANTHROPIC_API_KEY). |
| `CLAUDE_MODEL` | `sonnet` | Model alias for CLI mode (`sonnet`, `opus`, `haiku`). |
| `CLAUDE_SYSTEM_PROMPT` |  | Extra system prompt prepended to every conversation. |
| `CLAUDE_TIMEOUT` | `180` | Seconds before a single Claude turn is abandoned. |
| `MAX_TOKENS` | `4096` | Upper bound on output tokens per API-mode reply. |
| `SYSTEM_PROMPT` | `You are a helpful AI assistant. Be concise unless asked for detail.` | Legacy name for CLAUDE_SYSTEM_PROMPT, read as a fallback because `.env.example` documented it for a long time while the code ignored it. |
| `TELECHAT_HOME` |  | Data home. Everything — `.env`, `bot.db`, logs, the PID file — resolves here. Defaults to `~/.telechat`. |

## Telegram

The primary adapter.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_ALLOWED_USER_IDS` |  | Comma-separated numeric user IDs allowed to talk to the bot. Empty means anyone can. |
| `TELEGRAM_BOT_TOKEN` |  | Bot token from @BotFather. Required for the Telegram adapter. |
| `TELEGRAM_CHAT_ID` |  | Chat the bridge posts cards to. Falls back to the first entry of TELEGRAM_ALLOWED_USER_IDS. Read from the `.env` file, not the process environment. |

## WhatsApp

Via the Green API bridge.

| Variable | Default | Purpose |
|---|---|---|
| `GREEN_API_BASE_URL` | `https://api.green-api.com` | Green API endpoint, for self-hosted or regional instances. |
| `GREEN_API_INSTANCE_ID` |  | Green API instance id. Required for the WhatsApp adapter. |
| `GREEN_API_TOKEN` |  | Green API token. |
| `POLL_INTERVAL_SECONDS` | `2` | Seconds between Green API receive polls. |
| `WHATSAPP_ALLOWED_NUMBERS` |  | Comma-separated numbers allowed to message the bot. Empty means anyone can. |

## Slack

Socket Mode.

| Variable | Default | Purpose |
|---|---|---|
| `SLACK_ALLOWED_USER_IDS` |  | Comma-separated Slack user IDs allowed to use the bot. Empty means anyone in the workspace can. |
| `SLACK_APP_TOKEN` |  | `xapp-` app token for Socket Mode. |
| `SLACK_BOT_TOKEN` |  | `xoxb-` bot token. Required for the Slack adapter. |

## Discord

Gateway WebSocket. Needs the `discord` extra.

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_ALLOWED_USER_IDS` |  | Comma-separated Discord user IDs allowed to use the bot. Empty means anyone who can see the bot can. |
| `DISCORD_BOT_TOKEN` |  | Bot token from the Discord developer portal. Required for the Discord adapter. |

## Web chat

The built-in browser UI.

| Variable | Default | Purpose |
|---|---|---|
| `WEB_CHAT_ALLOW_OPEN` |  | Explicitly permit an exposed, unauthenticated web chat. Refuses to start without this. |
| `WEB_CHAT_AUTH_LOCKOUT_SEC` | `300` | How long that lockout lasts. |
| `WEB_CHAT_AUTH_MAX_ATTEMPTS` | `5` | Failed token attempts from one IP before it is locked out. |
| `WEB_CHAT_BIND` | `127.0.0.1` | Bind address. Defaults to `127.0.0.1`; binding wider requires WEB_CHAT_TOKEN or WEB_CHAT_ALLOW_OPEN. |
| `WEB_CHAT_PORT` | `8585` | Port for the built-in web UI. |
| `WEB_CHAT_TOKEN` |  | Access token required to open the web UI. Strongly recommended whenever it is not on loopback. |
| `WEB_CHAT_TRUST_PROXY` | `0` | Trust `X-Forwarded-For` for client IPs. Only set this behind a proxy you control. |

## Invites and groups

Handing access to someone else, and behaviour in group chats.

| Variable | Default | Purpose |
|---|---|---|
| `GROUP_DEFAULT_MODE` |  | How the bot behaves in a group it has no setting for: `mention` (default — only when @mentioned, replied to, or commanded), `all`, or `off`. Per-chat overrides come from `/groupmode`. |
| `INVITE_ALLOW_CHAINING` |  | Let users who were themselves invited mint invites of their own. Off by default, so invites fan out one level from you and a link cannot spread without you. |

## Claude Desktop bridge

Hooks that push your desktop Claude sessions to your phone.

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_APPROVAL_TIMEOUT` | `300` | Seconds an approval card waits for a tap before the timeout policy applies. |
| `BRIDGE_APPROVAL_TIMEOUT_ACTION` | `fallthrough` | What an unanswered approval resolves to: `fallthrough` (default, hands back to Claude Code), `deny`, or `allow`. |
| `TELECHAT_BRIDGE_INTERNAL` |  | Set by the bridge on processes it spawns, so their own hooks don't post duplicate cards. Not for operators. |

## Storage and limits

Database, rate limiting, write-queue behaviour.

| Variable | Default | Purpose |
|---|---|---|
| `DB_HISTORY_WAIT_TIMEOUT` | `2.0` | Seconds a history read waits for pending writes so it doesn't serve a conversation missing its newest turns. |
| `DB_PATH` |  | SQLite database path. Defaults to `$TELECHAT_HOME/bot.db`. |
| `DB_WRITE_MAX_ATTEMPTS` | `3` | Retries for a write that fails on lock/busy contention. |
| `DB_WRITE_QUEUE_TIMEOUT` | `5.0` | Seconds a caller waits for room in a full write queue before writing synchronously. |
| `MAX_CONCURRENT_TASKS` | `5` | Claude turns allowed to run at once across all users. |
| `RATE_LIMIT_REQUESTS` | `20` | Messages allowed per user per window. |
| `RATE_LIMIT_WINDOW` | `60` | Length of that window, in seconds. |

## Memory and knowledge base

Long-term recall and RAG.

| Variable | Default | Purpose |
|---|---|---|
| `AUTO_MEMORY` | `true` | Extract memories from conversations automatically. |
| `AUTO_MEMORY_MIN_LENGTH` | `100` | Shortest message considered worth extracting from. |
| `KB_CHUNK_OVERLAP` | `200` | Overlap between adjacent chunks. |
| `KB_CHUNK_SIZE` | `1000` | Characters per indexed chunk. |
| `KB_ENABLED` | `true` | Enable the knowledge base / RAG retrieval. |
| `KB_MAX_CONTEXT_CHARS` | `4000` | Hard cap on retrieved context, whatever the chunk count. |
| `KB_MAX_CONTEXT_CHUNKS` | `5` | Chunks retrieved per query. |

## Cost and routing

Budgets, model selection, the two-agent planner.

| Variable | Default | Purpose |
|---|---|---|
| `COST_BUDGET_ENABLED` | `true` | Enforce spend budgets. In API mode this is what makes `/budget` mean anything. |
| `COST_DAILY_BUDGET` | `5.0` | US dollars per day, per user. |
| `COST_MONTHLY_BUDGET` | `50.0` | US dollars per month, per user. |
| `COST_WARN_THRESHOLD` | `0.8` | Fraction of a budget (0–1) at which the user is warned. |
| `EXECUTOR_MODEL` |  | Model that executes each step. |
| `MODEL_HAIKU` | `claude-haiku-4-5` | Override the registry's Haiku id. |
| `MODEL_OPUS` | `claude-opus-5` | Override the registry's Opus id. |
| `MODEL_SONNET` | `claude-sonnet-5` | Override the registry's Sonnet id. |
| `PLANNER_MODEL` |  | Model that writes the plan. |
| `SMART_ROUTE_HAIKU_API` |  | Model id used for the Haiku tier in API mode. |
| `SMART_ROUTE_HAIKU_MAX` | `50` | Complexity score at or below which Haiku is used. |
| `SMART_ROUTE_OPUS_API` |  | Model id used for the Opus tier in API mode. |
| `SMART_ROUTE_OPUS_MIN` | `200` | Complexity score at or above which Opus is used. |
| `SMART_ROUTE_SONNET_API` |  | Model id used for the Sonnet tier in API mode. |
| `SMART_ROUTING_ENABLED` | `true` | Pick the model per message from estimated complexity. |
| `TWO_AGENT_ENABLED` | `true` | Enable the planner/executor split behind `/plan`. |
| `TWO_AGENT_MAX_STEPS` | `10` | Executor steps before the plan is abandoned. |
| `TWO_AGENT_THRESHOLD` | `100` | Complexity above which a message is routed to the two-agent path. |

## Media and web

Voice, images, music, video, search, fetch, documents.

| Variable | Default | Purpose |
|---|---|---|
| `BRAVE_SEARCH_API_KEY` |  | Brave Search key. |
| `BROWSER_ENABLED` | `false` | Enable Playwright browser automation. |
| `BROWSER_HEADLESS` | `true` | Run that browser headless. |
| `BROWSER_TIMEOUT` | `30000` | Per-action browser timeout in milliseconds. |
| `EXTRACT_MAX_SIZE_MB` | `50` | Largest document accepted. |
| `EXTRACT_MAX_TEXT_CHARS` | `500000` | Characters kept from an extracted document. |
| `GROQ_API_KEY` |  | Groq API key. Groq serves Whisper on a free tier, so this is all voice transcription needs — setting it switches transcription on by itself. Get one at https://console.groq.com/keys. |
| `IMAGE_GEN_ENABLED` | `false` | Enable image generation. |
| `IMAGE_GEN_MODEL` | `dall-e-3` | Image model id. |
| `IMAGE_GEN_QUALITY` | `standard` | Generated image quality tier. |
| `IMAGE_GEN_SIZE` | `1024x1024` | Generated image size. |
| `JINA_API_KEY` |  | Jina Reader key. With it, fetches go through Jina; without it, raw HTML is fetched and stripped. |
| `LINK_FETCH_TIMEOUT` | `10` | Per-link timeout in seconds. |
| `LINK_MAX_CONTENT_KB` | `50` | Kilobytes read from each such link. |
| `LINK_MAX_LINKS` | `3` | Links followed per message. |
| `LINK_UNDERSTANDING_ENABLED` | `true` | Automatically read links users send. |
| `MUSIC_GEN_DURATION` | `10` | Seconds of audio generated. |
| `MUSIC_GEN_ENABLED` | `false` | Enable music generation. |
| `MUSIC_GEN_MODEL` | `meta/musicgen:671ac645ce5e552cc63a54a2bbff63fcf798043055d2dac5fc9e36a837eedcfb` | Music model id. |
| `OPENAI_API_KEY` |  | OpenAI key, used for TTS and image generation. |
| `REPLICATE_API_TOKEN` |  | Replicate token, used for music and video generation. |
| `SCREENSHOT_DIR` |  | Where browser screenshots are written. |
| `TAVILY_API_KEY` |  | Tavily key, the other supported search backend. |
| `TRANSCRIPTION_ENABLED` |  | Transcribe inbound voice messages. Leave unset to let a `GROQ_API_KEY` decide on its own; `false` always wins. An `OPENAI_API_KEY` alone will not enable it, because TTS and image generation share that key. |
| `TRANSCRIPTION_MAX_SIZE_MB` |  | Largest voice file accepted for transcription. |
| `TRANSCRIPTION_PROVIDER` |  | Which transcription service to use: `auto` (default — prefers the free one), `groq`, or `openai`. Pinning a provider with no key configured disables transcription rather than quietly using the other one. |
| `TTS_ENABLED` | `false` | Enable text-to-speech replies. |
| `TTS_MAX_LENGTH` | `4096` | Longest text sent to the TTS API in one request. |
| `TTS_MODEL` | `tts-1` | Text-to-speech model id. |
| `TTS_VOICE` | `alloy` | Default voice. |
| `VIDEO_GEN_ENABLED` | `false` | Enable video generation. |
| `VIDEO_GEN_MODEL` | `luma/ray` | Video model id. |
| `WEB_FETCH_ENABLED` | `false` | Allow fetching and summarising URLs. |
| `WEB_FETCH_MAX_KB` | `100` | Kilobytes read from a fetched page before truncation. |
| `WEB_FETCH_TIMEOUT` | `15` | Per-fetch timeout in seconds. |
| `WEB_SEARCH_ENABLED` | `false` | Enable web search. |
| `WEB_SEARCH_MAX_RESULTS` | `5` | Results kept per search. |
| `WEB_SEARCH_PROVIDER` | `auto` | Search backend: `auto` picks whichever key is present, or name `brave` / `tavily`. |
| `WHISPER_MODEL` |  | Whisper model id for transcription. Leave empty for the right default per provider (`whisper-large-v3-turbo` on Groq, `whisper-1` on OpenAI); a model that only exists on the other provider is ignored rather than sent. |

## Self-improvement

Judge, preference learning, prompt A/B, auto-update.

| Variable | Default | Purpose |
|---|---|---|
| `AUTO_SCHEDULER_ENABLED` | `true` | Run the background self-improvement scheduler. |
| `JUDGE_MODEL` |  | Model that does the judging. |
| `JUDGE_SAMPLE_RATE` | `0.1` | Fraction of replies sampled for judging (0–1). |
| `PROMPT_OPTIMIZER_ENABLED` | `false` | A/B test system-prompt variants. |
| `PROMPT_PROMOTE_MARGIN` | `0.05` | Score margin required to promote a variant. |
| `PROMPT_PROMOTE_MIN_SAMPLES` | `20` | Samples a variant needs before it can win. |
| `SCHEDULED_TASKS_FILE` |  | Where those task definitions are stored. Defaults to a file in the data home. |
| `SCHEDULER_ENABLED` | `false` | Run user-defined scheduled tasks (`/schedule`). |
| `UPDATE_AUTO_APPLY` | `false` | Install updates automatically rather than only notifying. |
| `UPDATE_CHECK_INTERVAL` |  | Seconds between update checks. 0 disables them. |
| `UPDATE_NPM_PACKAGE` | `telechat` | npm package name the updater watches. |
| `UPDATE_PYPI_PACKAGE` | `telechatai` | PyPI package name the updater watches. |

## Operations

Health endpoint, debugging, MCP.

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_BIND_ADDR` | `127.0.0.1` | Bind address for it. Loopback by default — the endpoint is unauthenticated. |
| `HEALTH_PORT` | `8484` | Port for the `/health` endpoint. |
| `MCP_ALLOWED_COMMANDS` |  | Extra command names MCP servers may be launched with, beyond the built-in runtime list. |
| `MCP_ALLOWED_COMMAND_PATHS` |  | Strict mode: directory prefixes a resolved MCP executable must live under. |
| `MCP_ALLOW_ANY_COMMAND` | `0` | Disable the MCP command allowlist entirely. Understand what that means first. |
| `MCP_CONFIG_FILE` |  | Path to an `mcpServers` JSON config. |
| `MCP_ENABLED` | `false` | Connect to external MCP servers. |
| `TELECHAT_DEBUG` |  | Verbose logging, and tracebacks instead of friendly messages. |
| `WATCHDOG_INTERVAL` | `30` | Seconds between watchdog liveness checks. |

## Watchdog

`scripts/watchdog.py`, the optional self-healing supervisor.

| Variable | Default | Purpose |
|---|---|---|
| `WATCHDOG_BATCH_WINDOW` | `60` | Seconds of errors collected into one batch before diagnosing. |
| `WATCHDOG_BOT_SERVICE` | `com.claude.chat-bot` | launchd service label the watchdog restarts. |
| `WATCHDOG_CLAUDE_MODEL` | `sonnet` | Model the watchdog uses to diagnose and write fixes. |
| `WATCHDOG_DRY_RUN` | `false` | Diagnose and log, but never apply a fix or restart anything. |
| `WATCHDOG_ENABLED` | `true` | Master switch for the self-healing supervisor. |
| `WATCHDOG_FIX_COOLDOWN` | `1800` | Seconds to wait after a fix before attempting another. |
| `WATCHDOG_MAX_FIXES_HOUR` | `3` | Fixes it may apply in an hour, so a fix loop cannot run away. |
| `WATCHDOG_PROJECT_DIR` | `/Users/dev/projects/telechat` | Checkout the watchdog operates on. |
| `WATCHDOG_REGRESSION_WATCH` | `120` | Seconds a fix is watched for regressions before it is accepted. |
| `WATCHDOG_SCAN_INTERVAL` | `30` | Seconds between log scans. |

---

143 variables, generated from `telechat_pkg/*.py` and `scripts/*.py`.
