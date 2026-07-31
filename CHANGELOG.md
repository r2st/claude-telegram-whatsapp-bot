# Changelog

`telechat update` ships in-product, so this is where you find out what an
update changed. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [Semantic Versioning](https://semver.org/).

Entries are written for the person running the bot: what changes for you,
not which function moved.

## [Unreleased]

### Added

- **Discord is now a fifth platform.** `BOT_MODE=discord` (or add it to a
  comma-separated list, or use `all`) runs it alongside the others in the same
  process and against the same conversation store, so a session you started on
  Telegram is there when you ask from Discord. In a server the bot answers when
  you mention it; in a DM it answers anything. It speaks the same command set
  the Slack adapter does, under `!` — `!help`, `!model`, `!engine`, `!reset`,
  `!sessions`, `!remember`, `!recall`. Like every other adapter it is outbound
  only: discord.py opens a gateway WebSocket, so there is still no public URL,
  no tunnel, and nothing to expose.

  Install it with `pip install 'telechatai[discord]'`, set `DISCORD_BOT_TOKEN`,
  and — this one bites — enable the **Message Content Intent** in the Discord
  developer portal. Without it Discord connects happily and delivers every
  message with an empty body, so the bot looks alive and ignores you; `telechat
  init` says so, and the adapter names it explicitly if it happens.
  `DISCORD_ALLOWED_USER_IDS` is the allowlist, and as on every other platform,
  empty means anyone who can see the bot can use it.

### Fixed

- **A code block longer than one message came out broken.** Splitting a reply
  avoids breaking inside a code block, but a block bigger than the whole message
  limit cannot be moved out of the way — so the split landed inside it, leaving
  one message holding an unterminated fence and the next opening with a stray
  ```` ``` ````. On Telegram that could also fail the MarkdownV2 parse and drop
  the message to plain text. The block is now closed at the end of one message
  and reopened — with its language — at the start of the next. This affects
  Discord most (2 000 characters a message), but Telegram had it too.

- **"Lost connection to Claude" when the `claude` CLI was simply not installed.**
  Starting the CLI can fail before there is anything to connect to, and only the
  synchronous path handled that. The async path — the one Telegram, WhatsApp,
  Slack and the web chat all use — let the error escape, so the web chat
  reported a network blip and invited you to try again, which could never work.
  You now get told which of the two problems it is: the binary is missing, or
  `CLAUDE_CLI_WORK_DIR` points somewhere that isn't a directory.
- **A failed Claude run pasted raw stderr at you.** `[Claude error]` followed by
  whatever the CLI printed — and, when it printed nothing, followed by nothing at
  all. The common failures now lead with what to do about them: signed out
  (`claude auth login`), usage limit reached, out of credit, model name rejected,
  permission denied. The original text is still underneath, and still in the log;
  an unrecognised failure is still shown verbatim.

- **A stray `**` could replace a heading with `NUL BOLD0 NUL` in the message.**
  Bold, links, code and headings are set aside behind markers while the rest of
  the text is escaped, then put back. Putting them back was one pass per kind in
  a fixed order, so a marker that ended up *inside* a span restored earlier was
  never looked at again and went out literally. One unclosed `**` anywhere in a
  reply was enough to trigger it, because the bold rule then runs forward and
  swallows whatever marker it meets. Every marker is now expanded wherever it
  ends up.
- **Long replies spent far too long being formatted.** Restoring those same
  markers rescanned the whole message once per marker, so the cost grew with
  markers × length — quadratically, in the one case that matters. A 59 000-character
  reply took 54 ms to format; it now takes 4 ms, and the cost grows in step with
  the message instead of racing ahead of it.

- **A link in a message could reach addresses on your own network.** Link
  understanding fetches any URL you paste, and only the URL itself was checked —
  redirects were followed automatically and never re-checked, so a public link
  answering `302 Location: http://169.254.169.254/…` (cloud metadata) or
  `http://127.0.0.1:8484/` (this bot's own health endpoint) was fetched, and the
  response went to Claude as context. Only literal IP addresses were rejected
  too, so any *hostname* pointing at a private address passed. Redirects are now
  followed one hop at a time with every hop re-checked, hostnames are resolved
  and every resolved address is checked, and the same rules apply to `/fetch`.
- **Smart routing sent refactors and debugging to Haiku.** Two rules ran
  before any complexity check: "five words or fewer is simple", and a
  simple-keyword rule. So "Refactor this codebase" was routed on its length, and
  "Refactor the payment pipeline and convert it to async" was routed on the word
  *convert*. Complexity signals are checked first now. A misroute is silent —
  you get a worse answer with no indication why — and with `SMART_ROUTING`
  enabled this affected every message.
- **Headings came out italic on Telegram, and blockquotes did not exist.** A
  heading was converted to `*text*`, which the italic pass then picked up and
  turned into `_text_`; and the `>` of a blockquote was escaped to `\>`, which
  renders as a literal greater-than sign. Both appear in most non-trivial Claude
  replies. Headings are bold now, quotes are quotes, and `####`–`######` are
  recognised instead of leaving their hashes in the text.
- **Long replies were split into far more messages than necessary.** The
  chunker took the first paragraph break past a third of the message limit
  instead of the last one, so a reply made of short paragraphs — which is most
  replies — went out in chunks of about 1 200 characters against a 4 000-character
  budget. A 7 800-character answer was five Telegram messages; it is now two.
- **A code block could be split across two messages, unterminated.** The break
  finder matched the ``` that closes a block as readily as the one that opens
  it, so one message ended holding an open fence and the next began with a stray
  closing one — broken formatting, and under MarkdownV2 sometimes a parse
  failure that dropped the message to plain text. Breaks now land on the start
  of a block, so the block moves whole into the next message.
- **`telechat doctor` did not exist if you installed with npm.** The command
  was documented and implemented, but the npm wrapper — the CLI that `npm
  install -g telechat` puts on your PATH — answered "Unknown command". Anything
  the Python backend owns is now forwarded through one list, so a command
  cannot be reachable on one install path and missing on the other.
- **The web chat printed a QR code your phone could not use.** Under "Scan to
  open on your phone" it always encoded this machine's LAN address, but the web
  chat binds to `127.0.0.1` unless you opt out — so scanning it timed out. The
  QR now appears only when the server is actually reachable from the network;
  otherwise you get a line saying it is local-only and which two settings make
  it shareable.
- **An aborted startup could lose queued database writes.** Only Ctrl-C drained
  the write queue, so a boot that failed on a missing token — or on an adapter
  raising on the way up — left rows in the queue and killed the writer's
  database connection on the way out. Every exit path drains it now.
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

- **`telechat web`** — starts just the local web chat, with no messenger
  account and no `.env` at all. It forces web-only mode for that one run
  (nothing is written to disk, so a Telegram setup is untouched), checks first
  that there is actually a way to reach Claude, and tells you what to install
  if there isn't. `--port N` moves it off 8585. This is the shortest path from
  "heard about it" to "talked to it".
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
