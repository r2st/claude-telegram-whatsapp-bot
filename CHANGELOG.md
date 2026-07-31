# Changelog

`telechat update` ships in-product, so this is where you find out what an
update changed. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

Entries are written for the person running the bot: what changes for you,
not which function moved.

## [Unreleased]

### Fixed

- **`SYSTEM_PROMPT` and `CLAUDE_CLI_ADD_DIRS` did nothing.** `.env.example`
  documented both names; the code reads `CLAUDE_SYSTEM_PROMPT` and
  `CLAUDE_ADD_DIRS`. Anyone who set a custom system prompt or extra Claude
  directories from the template was silently ignored. Both legacy names are now
  read as fallbacks, so existing `.env` files start working — no edit required.
  The template documents `CLAUDE_API_MODEL` for API mode as well; `CLAUDE_MODEL`
  is the CLI alias and was never the API model.
- **Starting the bot could kill unrelated processes.** `pgrep -f
  telechat_pkg.main` matched an editor with `main.py` open, a `grep` for the
  string, or a test run — and SIGTERMed every match, for any user. The bot now
  records itself in `~/.telechat/.telechat.pid` (the file the npm wrapper
  already used, so `telechat stop` and a pip-started bot finally agree) and
  verifies each candidate's own command line before signalling it. A health port
  held by something that is not telechat is reported instead of killed.
- **Long-response pagination expired far sooner than it should have.** The
  in-memory response store capped itself by counting paginated responses and
  pending Retry stashes together, so a handful of unpressed Retry buttons made
  every new response evict an older one. "Page expired" after a few messages
  instead of fifty.
- **The web chat's brute-force defence could exhaust memory.** A per-IP failure
  counter was only removed on a successful login, so one failed attempt each
  from many addresses left an entry per address behind forever. Expired windows
  are now pruned and the table is capped — without letting an attacker evict
  their own counter by filling it.
- **A knowledge-base search containing a `"` fell back to a full table scan.**
  The FTS query builder quoted each word but never escaped an embedded quote,
  so `a"b` produced unterminated syntax, `MATCH` raised, and the search
  silently degraded to `LIKE`. `memory.py` had always escaped correctly.
- **Two WhatsApp messages arriving together in one chat could bypass the
  one-turn-at-a-time lock**, since the per-chat lock was created with a
  check-then-act that two threads could both win.
- **MCP servers were vetted by name only.** The allowlist compared the command's
  basename, so a script called `npx` planted in any world-writable directory on
  `PATH` passed. Commands are now resolved to an absolute path, refused if that
  path or its directory is world-writable, and the resolved path is what gets
  executed.
- **A failed Claude call in web chat showed the raw exception to the browser**
  — SDK/network error text (hosts, status bodies, sometimes header values)
  verbatim, prefixed "Error: Error: " (the client already adds one "Error: ").
  Replaced with a small classifier that turns timeouts, rate limits, auth
  failures, and connection errors into a plain-language message; the real
  exception still goes to the server log.

### Added

- **`telechat doctor`** — the same configuration/connectivity checks the
  Telegram `/doctor` command runs, now reachable from the CLI for when the bot
  won't start in the first place. Also flags settings in `.env` that nothing in
  telechat reads (the `SYSTEM_PROMPT`/`CLAUDE_SYSTEM_PROMPT` kind of typo, so
  it's caught immediately instead of looking like a broken feature).
- **`/export` in the web chat UI**, matching the Telegram command: downloads
  the current conversation as text, Markdown, HTML, or JSON directly from the
  browser.
- **`docs/configuration.md`** — every one of the 137 environment variables the
  code reads, with its real default. Generated from source and checked in CI, so
  it cannot fall behind again.
- **Bridge approval timeout policy.** `BRIDGE_APPROVAL_TIMEOUT` and
  `BRIDGE_APPROVAL_TIMEOUT_ACTION` (`fallthrough` | `deny` | `allow`). The
  default is the previous fail-open behaviour; `deny` is for when you turn
  approval on *because* you are away from the machine. Approval cards now state
  what inaction will do.
- **`MCP_ALLOWED_COMMAND_PATHS`** — opt-in strict mode restricting MCP
  executables to named directory prefixes.
- **`scripts/dev-setup.sh`** — one command to a working development environment.
- **`/health` now reports the database write path** — writer liveness, queue depth,
  retries, permanently dropped writes, and synchronous fallbacks. The endpoint
  returns 503 when the writer thread has died with a live queue, which is the
  one condition that means writes are degraded *right now*. `store.py` had been
  counting most of this since its queue was fixed; nothing could read it.
- **Web chat now has a dark theme, a real social-share preview, and is
  keyboard/screen-reader accessible.** The page picks up the browser's
  `prefers-color-scheme` automatically; sharing a TeleChat link now renders a
  title, description, and card on Slack/Twitter/Dev.to instead of a bare URL;
  and the suggestion/command chips are real `<button>`s (focusable, work with
  Enter/Space) with `aria-live` regions announcing connection state and new
  messages, plus `prefers-reduced-motion` support.

### Changed

- **`docs/architecture.md` described a different program** — `src/main.py`,
  FastAPI, APScheduler, an entry point called `claude-telegram-bot`. None of it
  exists here, and `AGENTS.md` points every contributor and agent at that file.
  Rewritten from the source and checked by tests.
- **`BOT_MODE=web` is documented.** A complete browser UI with token auth and a
  QR code has shipped for a while and the README never mentioned it once.
- Failures that are survivable are no longer silent: roughly 30
  `except Exception: pass` handlers now log at a level matched to how much they
  matter. Most visibly, the bridge watcher could fail on every pass forever
  while looking exactly like a quiet one.
- Lint (`ruff`) and a pytest configuration now run in CI. The first pass found a
  duplicated test class that shadowed another and had never run, ~130 unused
  imports, and a handful of dead locals.

## [1.2.0]

### Added

- Self-improving loop: LLM-as-judge scoring, per-user preference learning,
  system-prompt A/B testing, and an auto-update checker.
- Claude Desktop bridge: hooks push your desktop Claude sessions to Telegram as
  triaged cards, replies inject back via `claude --resume`, and Bash/Write/Edit
  can be gated behind a phone approval.
- Slack adapter (Socket Mode) and a local web chat UI.

### Fixed

- **Every API-mode turn was recorded as costing $0**, so `/budget` enforced
  nothing. Pricing now lives in `models.py` alongside the model registry.
- **Default model IDs were past their announced retirement.** All model IDs come
  from one registry and can be overridden per tier.
- **Three different version numbers shipped in one release** (pyproject 1.2.0,
  `__init__` 1.1.5, npm 1.1.1), which left the updater nagging npm users
  permanently. Every version now derives from `pyproject.toml`, checked in CI.
- **The Docker image could not build** — the Dockerfile still copied loose
  modules from the repository root, including one that never existed. It now
  installs the package, runs as a non-root user, and declares a healthcheck. CI
  builds it.
- **The database could silently lose writes.** The writer thread dropped whole
  batches on any error, leaked its connection, and never shut down; a full queue
  inverted write ordering; `save_turn` was three unrelated writes so a crash
  could half-apply it; sub-millisecond turns overwrote each other; and history
  reads could serve a conversation missing its newest turns, which presented as
  the bot forgetting what you had just said.
- The Python floor is 3.10, which is what the code actually requires — 3.9
  installs failed in ways that looked like bugs elsewhere.

## [1.1.x] and earlier

Initial public releases: Telegram and WhatsApp adapters, CLI and API modes,
multi-session conversations, FTS5 memory, knowledge base, cost budgets, smart
model routing, media generation, MCP, and the watchdog. See
`git log` for the detail — this changelog starts at 1.2.0.
