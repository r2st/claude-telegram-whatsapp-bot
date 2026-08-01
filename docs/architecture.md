# Telechat architecture

> **Rewritten 2026-07-31.** The previous version of this file described a
> different program entirely — `src/main.py`, FastAPI, APScheduler, aiosqlite,
> `claude-agent-sdk`, version 1.6.0, an entry point called
> `claude-telegram-bot`. Not one of the modules it named exists in this
> repository. `AGENTS.md` points every agent here as the canonical
> architecture overview, so that document was actively misleading its readers.
> This one is derived from the source at `telechat_pkg/` and is checked against
> it by `tests/test_architecture_doc.py`.

One process, several chat adapters, one Claude invocation layer, one SQLite
file. That is the whole shape of it. Everything below is detail.

```
Telegram ─┐
WhatsApp ─┤                                    ┌─ claude CLI  (subprocess)
Slack    ─┼─→ adapter ─→ claude_core.ask_* ────┼─ Anthropic API (httpx)
Web chat ─┘       │                            └─ claude-code-sdk
                  ↓
              store.py  ──→  ~/.telechat/bot.db  (SQLite, WAL)
```

## Entry point and process model

`telechat` (the console script) → `telechat_pkg/main.py:cli_entry`, which
dispatches subcommands (`init`, `start`, `bridge`, `help`, `--version`).

`_cmd_start()` is the composition root:

1. Resolve the data home (`$TELECHAT_HOME`, else `~/.telechat`) and `chdir`
   there, so `.env`, `bot.log`, and `bot.db` resolve identically no matter
   where the command was run from.
2. Load `.env`, configure logging (console at WARNING, rotating file at INFO or
   DEBUG).
3. Parse `BOT_MODE` into a set of platforms. `both` and `all` are aliases.
4. Replace any instance already running — see [Single instance](#single-instance).
5. `asyncio.run(_main())`, which starts the health server, the update checker,
   and one runner per configured platform.

**Concurrency is deliberately mixed**, because the libraries are:

| Platform | How it runs | Why |
| --- | --- | --- |
| Telegram | On the main asyncio loop (`run_telegram`) | `python-telegram-bot` is async; this is the primary adapter and gets the loop |
| Web chat | asyncio task on the same loop (`run_web_chat`) | aiohttp |
| WhatsApp | Daemon thread (`_run_whatsapp`) | Green API is a blocking polling loop |
| Slack | Daemon thread (`_run_slack`) | `slack_bolt` Socket Mode is blocking |
| Health | Daemon thread (`http.server`) | Trivial, and must answer even if the loop is busy |
| Bridge watcher | Daemon thread (`_watcher_loop`) | Polls transcript files on a timer |

Which is why `store.py` is written to be thread-safe rather than
loop-affine — see below.

## Modules

| Area | Modules | Responsibility |
| --- | --- | --- |
| Entry / CLI | `main.py`, `__main__.py` | Subcommand dispatch, the `init` wizard, `.env` handling, single-instance enforcement, startup |
| Claude invocation | `claude_core.py`, `models.py` | The three ways to reach Claude, prompt assembly, streaming callbacks, output parsing, the model/pricing registry |
| Persistence | `store.py`, `session_manager.py` | SQLite connection handling, the async write queue, history cache, rate limiting, usage and cost tracking, multi-session state |
| Adapters | `telegram_bot.py`, `whatsapp_bot.py`, `slack_bot.py`, `web_chat.py` (+ `web_chat_ui.html`) | Per-platform message handling, commands, access control, rendering |
| Desktop bridge | `desktop_bridge.py`, `bridge_evidence.py` | Claude Code hooks → Telegram cards, replies → `claude --resume`, tool approval, session following; card evidence (files/tests/errors) parsed from the transcript |
| Recall | `memory.py`, `knowledge_base.py`, `context_compaction.py` | FTS5 memories, chunked document store, history summarisation |
| Cost and routing | `cost_budget.py`, `smart_router.py`, `two_agent.py` | Budgets, per-message model selection, the planner/executor split |
| Self-improvement | `evaluator.py`, `preferences.py`, `prompt_optimizer.py`, `feedback.py`, `updater.py`, `auto_scheduler.py` | LLM-judge scoring, learned preferences, prompt A/B, update checks |
| Media and web | `voice_transcription.py`, `tts.py`, `image_gen.py`, `music_gen.py`, `video_gen.py`, `web_search.py`, `web_fetch.py`, `link_understanding.py`, `document_extract.py` | Optional capabilities, each guarded by its own `is_available()` |
| MCP | `mcp_client.py`, `mcp_tools.py` | Server connections and the JSON-RPC transport; converting discovered tools into Anthropic tool definitions and running the tool-use loop |
| Infrastructure | `health.py`, `event_bus.py`, `resource_limiter.py`, `error_classifier.py`, `text_chunking.py`, `markdown_v2.py`, `qr_util.py` | Health/watchdog, in-process events, subprocess ceilings, error classification, formatting |

Optional features are imported inside `try/except ImportError` and expose
`is_available()`, so the bot runs with only the core dependencies installed and
degrades feature by feature rather than failing to start. `pip install
telechatai[all]` turns them all on.

## Claude invocation

`claude_core.py` offers three paths, selected by `CLAUDE_MODE`:

- **`cli`** (default, free with a Claude subscription) — spawns the `claude`
  binary with `--output-format stream-json`, reads the stream line by line, and
  turns tool-use and text events into `on_progress` / `on_text` callbacks. This
  is what drives the live "🔧 Reading main.py…" progress card.
- **`api`** — the Anthropic SDK, streaming text deltas through the same
  callbacks. Token counts come back from the API and are priced through
  `models.py`.
- **`sdk`** — `claude-code-sdk`, when installed. Permission mode is mapped from
  `CLAUDE_CLI_PERMISSION_MODE` rather than hardcoded.

All three return `(reply_text, stats)` with the same stats keys, so adapters do
not care which one ran. Sessions are continued with `--resume <session_id>`,
which is what makes a Telegram conversation a single Claude session rather than
a series of unrelated turns.

## Persistence

One SQLite file (`$TELECHAT_HOME/bot.db`, WAL mode), and every table lives in
it:

| Table | Written by | Holds |
| --- | --- | --- |
| `conversations` | `store.py` | Turn history per platform/user/session |
| `usage`, `tool_usage`, `cost_tracking` | `store.py` | Token counts, tool calls, spend |
| `sessions` | `store.py` | Claude session id per platform/user |
| `user_sessions`, `active_sessions` | `session_manager.py` | Named multi-session state |
| `feedback`, `quality_scores` | `store.py` | 👍/👎 and LLM-judge scores |
| `memories` | `memory.py` | FTS5-indexed long-term memory |
| `kb_documents`, `kb_chunks` | `knowledge_base.py` | Knowledge base and its chunks |
| `bridge_*` (6 tables) | `desktop_bridge.py` | Session cards, approvals, follow state, approve mode |

Three properties of `store.py` are load-bearing, and each was a bug first:

- **Writes go through a queue on a single writer thread**, batched into
  transactions. A `_WriteOp` carries all the statements of one logical write, so
  a multi-statement write (`save_turn`) can never half-apply. Permanent failures
  are logged with the offending SQL and dropped; transient lock/busy failures
  retry.
- **A full queue applies back-pressure** rather than letting the caller jump
  ahead — the old fallback inverted write ordering.
- **History reads wait for the writes they depend on.** The writer commits on
  its own connection, so a reader would otherwise serve a conversation missing
  its newest turns, which presents as the bot forgetting what was just said.
  Ops carry the cache keys they invalidate, and `load_history` waits on pending
  ones.

Connections are thread-local (`_get_conn`), with a busy timeout set *before*
the `journal_mode=WAL` pragma so concurrent first-opens don't race.

## The Claude Desktop bridge

The differentiator, and the part with the most moving pieces.

`telechat bridge install` writes hook entries into `~/.claude/settings.json`:

```
Stop, Notification, SubagentStop  →  telechat bridge notify <event>
PreToolUse (with --approval)      →  telechat bridge approve
```

Each hook is a *separate short-lived process* — Claude Code runs it, it does its
work against the same `bot.db`, and exits. That is why bridge state is in the
database rather than in memory: the notifying process and the Telegram poller
that later handles your reply are different processes.

```
Claude Code session ends a turn
        │  Stop hook
        ▼
telechat bridge notify stop ──→ read transcript ──→ AI triage digest (haiku)
        │                                                    │
        └────────────────→ Telegram card ←───────────────────┘
                                │
        your reply / button tap │  (handled by the running bot)
                                ▼
                  claude --resume <sid> -p "<your message>"
                        (TELECHAT_BRIDGE_INTERNAL=1 so the
                         resumed session's own Stop hook stays quiet)
```

The approval hook is synchronous: it writes a row to `bridge_approvals`, sends a
card with Approve/Deny buttons, and polls that row until a decision lands or
`BRIDGE_APPROVAL_TIMEOUT` expires. What an expiry means is
`BRIDGE_APPROVAL_TIMEOUT_ACTION` — `fallthrough` (default), `deny`, or `allow`.

A background watcher thread (`_watch_once`, polled every few seconds) streams
followed sessions' new turns to Telegram and posts session start/exit pings.

## Single instance

Starting the bot replaces any instance already running. The bot records itself
in `~/.telechat/.telechat.pid` — the same file the npm wrapper writes, so
`telechat stop` and a pip-started bot agree on what is running — and every
candidate for termination (from the PID file, from a `pgrep -u <uid>` fallback,
or from the health port) is checked against its own argv by
`_is_telechat_cmdline()` first. A process that merely *mentions* telechat (an
editor, a `grep`, a test run) is not a telechat process and is left alone.

## Access control

A flat allowlist per platform: `TELEGRAM_ALLOWED_USER_IDS`,
`WHATSAPP_ALLOWED_NUMBERS`, `SLACK_ALLOWED_USER_IDS`, and `WEB_CHAT_TOKEN`. An
empty allowlist means anyone can use the bot, and `telechat init` warns about
it.

Everyone who passes shares one Claude authentication, one working directory, and
one permission mode. Conversations and memory are keyed per user; **capability
is not.** See `SECURITY.md` — this is a single-operator tool, and that is a
design decision rather than an oversight.

The web chat binds `127.0.0.1` by default and refuses to start exposed without a
token; `X-Forwarded-For` is trusted only behind `WEB_CHAT_TRUST_PROXY`. MCP
server commands are resolved to an absolute path and refused if that path is
world-writable. The health endpoint is unauthenticated and binds loopback.

## Configuration

Environment variables, read from `$TELECHAT_HOME/.env` or the process
environment. **[`configuration.md`](configuration.md) is the complete
reference** — it is generated from the source by `scripts/env_reference.py` and
checked in CI, so it cannot drift.

## Testing

4,000+ tests under `tests/`, one file per module plus per-platform e2e suites.
CI runs the suite on Python 3.10–3.13 with a coverage floor, `ruff`, a
version-consistency check, a Docker build, and the configuration-reference
check. `tests/README_E2E.md` covers the recorded Anthropic cassettes.

## Where to add things

| You want to | Do this |
| --- | --- |
| Add a Telegram command | Handler in `telegram_bot.py`, register it in `build_app()` and in `BOT_COMMANDS` |
| Add a capability (a new generator, a new source) | New module with `is_available()` and an optional extra in `pyproject.toml`; import it guarded |
| Add a setting | Read it in the module that uses it, then `python scripts/env_reference.py` |
| Add a table | Create it in the owning module's schema function, called from `store.init_db()` |
| Change bridge behaviour | `desktop_bridge.py` — and read `agents/inbox/0023–0027` first; they are queued against it |
| Reach Claude a new way | `claude_core.py`, returning the same `(text, stats)` shape as the existing three |
