# Telechat — Code Review (CLOSED — historical record)

> ## ⛔ This document is closed. Do not work from it.
>
> **Superseded by [`docs/improvements.md`](improvements.md) as of 2026-07-30** (item 35).
> It is kept only as a historical record of the 2026-05-20 review.
>
> **Why it is closed rather than updated:** its structure still reads as an open P0
> list, but of its seven P0 items five are fully fixed, several
> `[PARTIALLY RESOLVED]` annotations are now fully resolved, and its line
> references are stale throughout (it cites `main.py` at 1083 lines; the file is
> 852). Every agent who trusted it therefore paid a re-verification pass to
> discover most of it was history. `docs/improvements.md` re-derived the open
> findings against current code, and its
> [Appendix A](improvements.md#appendix-a--code_reviewmd-findings-verified-fixed)
> is the disposition record for everything here.
>
> **What is still worth reading:** §1's component map is accurate and is a good
> starting point for rewriting `docs/architecture.md` (improvements.md item 19).
> Treat every *finding* below as closed unless improvements.md carries it forward.

Reviewed: 2026-05-20. Scope: `telechat` (telechatai 1.1.5 at the time; 1.2.0 now).
No code was modified; this was a read-only review.

The inline **[RESOLVED …]** / **[PARTIALLY RESOLVED …]** / **[OUT OF DATE: …]**
markers below were added 2026-06-03 by ticket 0020 and were never completed —
which is part of why the document is closed rather than maintained.

---

## 1. Project overview

**What it is.** `telechatai` is a self-hosted, multi-platform chat-bot frontend that proxies messages to Claude
(either via the `claude` Code CLI, the Anthropic API directly, or the `claude-code-sdk`). It exposes Claude
through Telegram, WhatsApp (Green API), Slack (Socket Mode), and an aiohttp-based web chat UI — all from a single
process selected via `BOT_MODE`.

**Architecture.**

- `telechat_pkg/main.py` (1083 lines, very large for an entry point) — CLI dispatcher (`init` / `start` /
  `help` / `--version`), interactive setup wizard with token validation, .env management, a 200-line hand-rolled
  QR encoder + Reed–Solomon implementation, process management (pgrep/lsof to kill prior instances), and the
  asyncio top-level driver.
- `telechat_pkg/__main__.py` — 3-line shim calling `main.cli_entry()`.
- `telechat_pkg/claude_core.py` — three Claude invocation paths (`ask_claude_sync`, `ask_claude_async` for CLI,
  `ask_claude_api_async`, `ask_claude_sdk`) plus re-exports of store internals for backwards compatibility.
- `telechat_pkg/store.py` — SQLite layer (thread-local connections, WAL, background writer thread, history
  cache, rate limiting, `UserSession` / `SessionManager`).
- `telechat_pkg/memory.py` — separate `MemoryStore` opening the same SQLite file with its own thread-local
  connection pool and FTS5 index.
- `telechat_pkg/session_manager.py` — a *second*, parallel session browser (`SessionBrowser`) that queries a
  `history` table that does **not exist** in the schema defined by `store.py`.
- `telechat_pkg/{telegram_bot,whatsapp_bot,slack_bot,web_chat}.py` — platform adapters.
- ~30 feature modules: memory, MCP client, knowledge base, scheduling, voice transcription, image/music/video
  gen, browser automation, web search/fetch, document extract, two-agent, cost budget, smart router, doctor,
  health, coder, conversation export, polls, TTS, etc.
- `scripts/` — install / watchdog / e2e shell scripts; `npm/` — the npm publishing wrapper.

**Entry points.** `pyproject.toml` exposes `telechat = "telechat_pkg.main:cli_entry"`. Default subcommand is
`start`. Runtime data home is `~/.telechat/` (DB, `.env`, logs).

---

## 2. Critical bugs

**[CRITICAL]** `telechat_pkg/session_manager.py:85,99,131,160,175,196` — Queries a non-existent `history` table.
`store.py` defines `conversations(platform,user_id,role,content,ts)`, **not** `history(platform,user_id,user_text,bot_reply,timestamp,session_name,cost_usd)`. Every `SessionBrowser` method (`list_sessions`, `get_session_history`, `fork_session`, `search_sessions`) will raise `sqlite3.OperationalError: no such table: history` the first time it's called. This is a wholly broken module.

**[RESOLVED 2026-06-03]** `_ensure_schema` now `CREATE TABLE IF NOT EXISTS user_sessions/conversations/active_sessions` on first connection (`session_manager.py:75-118`), and an idempotent legacy-history migration ports old `history`-table rows into the new shape (`session_manager.py:120-178`). Module is wired into `telegram_bot.py:2949-2950` and exercised by 13 tests in `test_new_features.py::TestSessionBrowser` — all passing.

**[CRITICAL]** `telechat_pkg/store.py:46-56` — Thread-local SQLite connections opened with
`check_same_thread=False` but the background writer thread (`_db_writer`) and request threads both call
`_get_conn()`. Each thread gets its own connection, which is correct, but the writer commits writes on its
*own* connection while other threads read via the cache on theirs; with WAL + `synchronous=NORMAL` this is
usually fine, however `replace_history`, `clear_history`, `delete_by_name`, `_save_session`, `_save_active`,
`_archive_session`, etc. all execute writes **directly on the calling thread's connection** (bypassing the
queue), so the same row can be modified by two different connections concurrently. With the cache layer in
`_history_cache` you get stale reads after a writer-thread INSERT/DELETE — there is no invalidation across
threads.

**[CRITICAL]** `telechat_pkg/store.py:65-85` `_db_writer` — Loops forever with no shutdown signal, never closes
`_get_conn()`, and on exception simply logs + continues; if a write fails mid-batch the remaining ops in `ops`
are silently lost (no retry, no DLQ). Combined with `Queue(maxsize=1000)` and `_enqueue_write` falling back to
synchronous write on Full (line 104-108), under back-pressure you get inconsistent ordering between async-queued
writes and sync writes — e.g. a sync DELETE can run *before* a queued INSERT it was meant to follow.

**[HIGH]** `telechat_pkg/store.py:288-304` `save_turn` — Two INSERTs and a cleanup DELETE are enqueued
*separately*. There is no atomicity guarantee — a crash or queue-full between the user-INSERT and
assistant-INSERT leaves the conversation half-saved. The DELETE uses `OFFSET 20` of the same table that
may not yet have the new row committed, so the trim window drifts.

**[HIGH]** `telechat_pkg/main.py:1011 / 1032 / 1034` — `os._exit(0)` in the SIGINT handler and on
KeyboardInterrupt skips all `atexit` hooks, the SQLite writer flush, `asyncio` task cancellation, and the
aiohttp `runner.cleanup()` in `web_chat.run_web_chat`. Pending DB writes in the queue are lost on Ctrl-C.

**[HIGH]** `telechat_pkg/claude_core.py:228-243` — Retry path on session resume failure rebuilds `cmd` with
`CLAUDE_WORK_DIR` (line 249) instead of the original `cwd` argument the caller passed in via `work_dir`. The
retry runs in the wrong directory.

**[HIGH]** `telechat_pkg/web_chat.py:53-55` — `X-Forwarded-For` is honored without any check for a trusted
proxy. Since `WEB_BIND` defaults to `0.0.0.0`, any direct client can spoof XFF and bypass the per-IP auth
throttling at `web_chat.py:62-82`. The lockout is per *claimed* IP, so an attacker just rotates the header.

**[HIGH]** `telechat_pkg/web_chat.py:220-222` — `asyncio.create_task(_handle_chat(...))` is fired without
keeping a reference and without bounding concurrency per connection. A malicious client can spam messages and
spawn unbounded concurrent Claude subprocess calls (each up to `CLAUDE_TIMEOUT=180s`). Task references can be
GC'd mid-flight (Python warns about this).

**[HIGH]** `telechat_pkg/memory.py:81-86, 218, 293, 320, 330` — `MemoryStore` opens its own thread-local
SQLite connection against the *same* `bot.db` as `store.py`. Two independent connection pools writing into the
same DB with WAL is workable, but **no journal_mode pragma** is set here, so it inherits whatever mode the file
is in. Worse, `_init_schema` creates triggers and FTS table on first call from whichever thread imports the
module — and may race with `store.init_db()` running concurrently in another thread.

**[HIGH]** `telechat_pkg/claude_core.py:47-52` — Module-level `__getattr__` that delegates to `_store` for any
unknown attribute can mask real `AttributeError`s and cause confusing debugging; combined with the fact that
many tests patch `cc._write_queue`, `cc._writer_thread`, etc., a patched attribute on `cc` will *not* reach
`store` — test mocks may silently miss.

**[HIGH]** `telechat_pkg/main.py:927-953` — On startup, the bot uses `pgrep -f telechat_pkg.main` to find and
SIGTERM existing instances. The match is on the substring `telechat_pkg.main` so any unrelated process whose
arguments contain that string (e.g. an editor, `grep`, a test harness) will be killed. Also matches
`telechat_pkg.main` running under a different user (no UID filter on `pgrep`).

**[MEDIUM]** `telechat_pkg/store.py:611-619` `get_active_index` — Returns 0 when no active session is found,
even if there are *no* sessions (callers may try to index `sessions[0]`).

**[MEDIUM]** `telechat_pkg/whatsapp_bot.py:442` — `if parent.is_dir() and str(parent).startswith(str(BROWSE_ROOT.parent))` is a string-prefix check, vulnerable to a sibling directory that shares a prefix (`/tmp/foo` vs `/tmp/foo-evil`). Use `Path.is_relative_to` or `os.path.commonpath`.

**[MEDIUM]** `telechat_pkg/whatsapp_bot.py:430-433` — `!cd <relative-path>` joins user input onto `cwd` and
re-lists *without* re-rooting under `BROWSE_ROOT`. A user can pass `../../..` and escape the browse sandbox
entirely. `!view` (line 455) also reads arbitrary files via numeric index from a list that was built before
the path validation logic.

**[MEDIUM]** `telechat_pkg/claude_core.py:425-426` — SDK path hardcodes `permission_mode="bypassPermissions"`,
ignoring the user-selected permission mode and the `perm_mode` arg in the API. Anyone using SDK engine gets
unrestricted tool execution.

**[MEDIUM]** `telechat_pkg/mcp_client.py:101` — `env = {**os.environ, **server.env}` then `create_subprocess_exec(server.command, ...)` with no PATH sanitization. If MCP config is user-controlled, this is straight command exec — see Security findings.

**[MEDIUM]** `telechat_pkg/store.py:295-298` — `save_turn` uses `INSERT OR IGNORE` keyed on `(platform,user_id,ts)` with `ts = now + 0.001` for assistant. On a fast machine two consecutive turns can collide on `ts` (sub-millisecond), silently dropping the second turn (IGNORE swallows the conflict).

**[MEDIUM]** `telechat_pkg/web_chat.py:307-308` — `_is_cancelled` checks `client_id not in _active_ws`, but
the dict is mutated from the `finally` clause in the WS handler. If the client disconnects, the active
subprocess is signaled via `proc.kill()` inside `_read_stream`, but the API/SDK paths just `break` the stream
and continue to `save_turn` etc. — partial replies get persisted as full assistant turns.

**[MEDIUM]** `telechat_pkg/main.py:1024-1034` — Global `_sigint_count` is not thread-safe; on a second Ctrl-C
during shutdown you race against the first.

**[MEDIUM]** `telechat_pkg/main.py:138-147` `_read_env` — Strips `value.strip()` but does not handle quoted
values, `export FOO=` prefixes, or escaped characters. `_set_env_var` writes plain `KEY=VALUE\n` — values
with `#`, `\n`, or whitespace round-trip incorrectly.

**[MEDIUM]** `telechat_pkg/web_chat.py:45` `_auth_failures` — Never bounded. Hostile traffic can grow this
dict unboundedly (one entry per spoofed IP), causing memory growth. No periodic GC.

**[LOW]** `telechat_pkg/claude_core.py:211` — `except (json.JSONDecodeError, Exception)` is redundant
(`Exception` swallows `JSONDecodeError`) — pattern repeated elsewhere.

**[LOW]** `telechat_pkg/store.py:282-283` — Cache eviction policy `_history_cache.clear()` is brutal; under
load you'll just keep clearing repeatedly. Use an LRU.

---

## 3. Security issues

**[HIGH]** `telechat_pkg/web_chat.py:34` — `WEB_BIND` defaults to `0.0.0.0` and `WEB_AUTH_TOKEN` defaults to
empty (`""`). When the user finishes setup without setting a web token, the bot **silently exposes a
fully-authenticated web chat to the LAN/internet** that issues Claude commands with whatever permission mode
is configured (often `auto` or `bypassPermissions`). The setup wizard prints a warning but does not fail
closed.

**[HIGH]** `telechat_pkg/web_chat.py:53-59` — `X-Forwarded-For` trusted blindly (see Section 2). Allows
bypass of rate limiting and forges audit logs.

**[HIGH]** `telechat_pkg/claude_core.py:425-426` — Hardcoded `permission_mode="bypassPermissions"` for the SDK
path. The bot will let Claude execute any tool (Bash, Write, Edit) without user confirmation when SDK engine is
selected.

**[HIGH]** `telechat_pkg/main.py:283, 301` — `_validate_telegram_token` uses
`urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe")` with the token in the URL path. If the
user enters a malformed value containing `/`, `?`, `&`, or whitespace, you get path/URL injection and at
minimum a misleading error; the same pattern repeats for Green API (line 203) — `instance_id` and `token` are
interpolated directly into the URL with no validation.

**[HIGH]** `telechat_pkg/mcp_client.py:102-108` — `create_subprocess_exec(server.command, *server.args, env=...)`
is fed straight from `MCP_CONFIG_FILE` JSON. If a user is tricked into loading a malicious MCP config (which
is a common attack vector for MCP tooling), this is arbitrary local code execution. No allow-list, no path
validation, no `shell=True` (good) but `command` could be `/bin/sh` with crafted args.

**[HIGH]** `telechat_pkg/whatsapp_bot.py:455-473` `!view` and `telechat_pkg/whatsapp_bot.py:430-435` `!cd` —
WhatsApp users can traverse and read **arbitrary files** on the host machine via path traversal (see Critical
section). `Path(arg).expanduser()` accepts absolute paths and `..` segments unchecked. With
`ALLOWED_NUMBERS` empty (default open mode), anyone messaging the WhatsApp number can read /etc/passwd, ssh
keys, the .env, etc.

**[HIGH]** `telechat_pkg/telegram_bot.py:48`, `whatsapp_bot.py:43,46`, `slack_bot.py:38,39` — Use
`os.environ["KEY"]` (KeyError on missing). Not exploitable, but combined with main.py's pre-flight check that
allows starting if *any* platform is configured, importing the wrong adapter module raises an unhandled
KeyError instead of a friendly error. (`main.py` does try to detect this for WhatsApp at line 920-925; not done
for Telegram or Slack.)

**[MEDIUM]** `telechat_pkg/main.py:184-225` — All three token validators silently swallow `Exception` (lines
194, 207, 224). Network errors and bad tokens are indistinguishable, leading users to "try again" loops.

**[MEDIUM]** `telechat_pkg/web_chat_ui.html:715` — `s.innerHTML = statsContent(stats)`. `statsContent` builds
the string from `stats.input_tokens.toLocaleString()` etc. — all numeric, OK. But `stats.tools_used` only has
its **length** used here; the rest of the UI uses `escapeHtml`/`renderMarkdown(text)`, where `renderMarkdown`
HTML-escapes the input first — generally safe. `safeUrl` is present and reasonable. **However**, `renderMarkdown`
emits `<pre><code>${code.trim()}</code></pre>` from the *already-escaped* HTML for fenced code blocks (line
781-782) — looks safe, but does not strip backticks inside, and the regex `\[([^\]]+)\]\(([^)]+)\)` can match
across HTML tags injected from earlier replacements (no real exploit found, but the rendering pipeline is
fragile — every order change risks bypass).

**[MEDIUM]** `telechat_pkg/web_chat.py:115` — `_index_handler` reads `web_chat_ui.html` from disk on
every request synchronously. Not strictly a security issue, but it does mean an attacker who can write to that
file (e.g. via a `Write`/`Edit` tool through Claude) can inject HTML into the UI. Cache in memory at startup.

**[MEDIUM]** `telechat_pkg/web_chat.py:184` — `hmac.compare_digest` is used (good), but the `_ip_is_locked`
check is keyed on possibly-spoofed IPs. Also, lockout never escalates past the window — once the window resets,
an attacker can immediately retry 5 more times. With a 5-min window and 5 attempts that's 60 attempts/hour;
against a short token (the wizard accepts any length), this is brute-forceable.

**[MEDIUM]** `telechat_pkg/main.py:78` — `_save_workdir` writes config file world-readable
(`open(..., "w")` default mode 0644). The file currently holds only `workdir` but the comment says it's shared
with the npm CLI, which may grow.

**[MEDIUM]** `.env` is gitignored (good), but **`bot.err` is not** (`*.err` is in `.gitignore` — actually it is,
line 27). `bot.log` and `bot.db*` similarly. ✓ — verified.

**[LOW]** `telechat_pkg/main.py:585-590` `_get_local_ip` opens a UDP socket to `8.8.8.8:80` — works but
leaks information about the host (used for QR display). Acceptable.

**[LOW]** `telechat_pkg/whatsapp_bot.py:107` — `f"{BASE_URL}/{path}/{API_TOKEN}"` puts the API token in the URL,
which Green API itself requires. Tokens will appear in any HTTP proxy/access log on the network. Not fixable
without Green API changing.

No `eval`, `exec`, `pickle.loads`, `os.system`, `shell=True`, `yaml.load`, or `verify=False` calls were found
in `telechat_pkg/`. SQL queries all use parameterized form — no string-built SQL.

---

## 4. Concurrency / async bugs

**[HIGH]** `telechat_pkg/store.py:128-147` `check_rate_limit` — Global `_rate_state` dict mutated from multiple
threads (Telegram async + WhatsApp worker threads + Slack thread + web ws tasks) **without a lock**. `setdefault`
+ list mutation is not atomic. Lost-update races are possible under load.

**[HIGH]** `telechat_pkg/store.py:473-828` `SessionManager` — `_cache`, `_active` dicts mutated from any thread
that calls into the store; **no lock**. All adapters share the singleton `_session_mgr`. Concurrent `create`
or `delete_by_name` will corrupt the in-memory cache. Visible bug: `delete_by_name` removes from
`sessions` list (`sessions.remove(sess)`) while another thread may be iterating it from `get_all`.

**[HIGH]** `telechat_pkg/memory.py:81-86` — `_init_schema` called from `__init__`, not idempotent across
processes; concurrent first-time initializers on the same DB will race on `CREATE TRIGGER IF NOT EXISTS` /
`CREATE VIRTUAL TABLE` — usually OK with SQLite's implicit locking, but the `ALTER TABLE` on line 112 is
unprotected and will throw if two instances race.

**[MEDIUM]** `telechat_pkg/web_chat.py:220-222` — Already noted: untracked `asyncio.create_task`s can be
garbage-collected. Should hold strong refs in a set.

**[MEDIUM]** `telechat_pkg/main.py:957-987` — WhatsApp and Slack run in `threading.Thread(daemon=True)`,
calling synchronous `requests.request(...)` (whatsapp_bot.py:109) which **blocks** — fine in a dedicated
thread, but `_session_mgr` calls inside that thread share state with the Telegram asyncio loop without a lock.
See above.

**[MEDIUM]** `telechat_pkg/store.py:88-95` `_ensure_writer` is racy on initial start — two threads calling
into the bot before `init_db` finishes could create two writer threads (the `is_alive()` check is on a possibly
stale reference). In practice `init_db()` is called once during boot, but worth tightening.

**[LOW]** `telechat_pkg/claude_core.py:174` — `proc.kill()` in `_read_stream` happens from inside the read
coroutine; the outer `await proc.wait()` is in the parent. If `kill` is called concurrently with `wait`, fine
on POSIX, but the read coroutine then returns without `await proc.wait()` so the parent's `wait` is what
reaps. OK but worth a comment.

---

## 5. Error handling smells

**[MEDIUM]** 82 occurrences of `except Exception:` across the codebase (50+ in `telegram_bot.py` alone). Many
silently `pass` after a callback failure. Spot examples:

- `telechat_pkg/claude_core.py:211, 273, 383, 450, 457` — JSON parse + everything else swallowed in tight loops.
- `telechat_pkg/main.py:194, 207, 224` — Token validators map "network down" and "bad token" both to
  "validation failed".
- `telechat_pkg/main.py:961, 968` — `_run_whatsapp` / `_run_slack` log.exception then return; the daemon
  thread dies silently; bot stays "running" but with one platform dead. No supervisor restart.
- `telechat_pkg/health.py:307` — bare `except Exception: pass` in a health-check helper.
- `telechat_pkg/markdown_v2.py:179`, `telechat_pkg/qr_util.py:14`, `telechat_pkg/link_understanding.py:72`,
  `telechat_pkg/scheduled_tasks.py:150,155,165` — same pattern.

**[MEDIUM]** `telechat_pkg/store.py:84-85` `_db_writer` — On batch failure logs and continues to next iteration,
silently dropping the failed ops. No retry, no metric.

**[MEDIUM]** `telechat_pkg/memory.py:488-490` — On API failure stores raw conversation prefix as a single
"memory", masking the bug. User won't know extraction is broken.

**[LOW]** Many handlers do `try: ... except Exception as e: send_message(..., f"Error: {e}")` which leaks
internal exception text to end-users (could include file paths, tokens in URLs, etc.). Web chat does this at
`web_chat.py:399`.

---

## 6. Code quality

**[HIGH]** `telechat_pkg/telegram_bot.py` is **3527 lines** — single-file god-module. It bundles dispatcher,
commands, settings panel, file browser, voice handling, image gen, video gen, music gen, polls, scheduling,
memory commands, session commands, coder integration, cost budget UI, etc. Should be split per-command-group.

**[NUMBER UPDATED 2026-06-03]** File is now **3604 lines** (grew slightly). The split work itself is still pending — no ticket claimed yet at time of this annotation.

**[HIGH]** `telechat_pkg/main.py` is **1083 lines** including ~250 lines of a hand-rolled QR encoder and
Reed–Solomon implementation (`_qr_encode_minimal`, `_rs_encode`) — duplicated against the `qrcode` library
which is already optionally imported (`main.py:601`) and also against `qr_util.py`. Pick one.

**[RESOLVED 2026-06-03 by ticket 0017]** Hand-rolled Reed–Solomon + QR encoder was removed before this branch (qr_util.py docstring confirms). The remaining ~60-line tail of dead `_get_local_ip` / `_print_web_qr` / `_render_qr_terminal` duplicates was deleted in ticket 0017 (commit `828f934`). `main.py` is now **844 lines**.

**[MEDIUM]** `claude_core.py` `ask_claude_async` (lines 116-288) duplicates a 50-line stream-reading block
for the retry path (lines 252-286). Same logic, different variable names. Extract a helper.

**[MEDIUM]** Two parallel session abstractions: `store.SessionManager`/`UserSession` (working, used by
adapters) and `session_manager.SessionBrowser`/`SessionInfo`/`ForkResult` (broken, no callers found via
grep — likely dead code).

**[MEDIUM]** `MemoryStore` (`memory.py`) and `store.py` open separate connection pools to the same DB file
with different journal settings. They could share `store._get_conn()`.

**[MEDIUM]** Type hints are partial. Public APIs in `claude_core.py` use `Optional[callable]` (`callable`
is a builtin function, not a type — should be `Callable[..., Awaitable[None]]`). Repeated at lines 125-127,
358-359, 403-405.

**[MEDIUM]** Many modules duplicate the env-flag idiom `os.getenv("FOO", "false").lower() in ("1","true","yes")`.
A `_bool_env` helper exists in some files but not others. Factor it.

**[LOW]** Inconsistent f-string vs `%`-formatting in log calls. Logging best practice is `log.info("%s", x)`
to avoid formatting cost on filtered levels — many files use f-strings instead.

**[LOW]** `telechat_pkg/main.py:36` global state (`_DATA_HOME`, `_CONFIG_FILE`) computed at import time means
setting `TELECHAT_HOME` after import has no effect. Tests work around this with monkeypatches.

---

## 7. Test suite health

- `pytest --collect-only -q` → **3068 tests collected, 0 collection errors** in 0.91s. ✓
- `def test_` count: **3158** total across 27 files. Roughly 3-to-1 test:source LOC (37k vs 15.6k).
- **Coverage-padding tests strongly suspected.** Files matching `test_100_*` / `test_coverage_*` /
  `test_full_coverage.py`:

  | File | tests | LOC |
  |---|---:|---:|
  | `tests/test_100_complete.py` | 61 | 1089 |
  | `tests/test_100_coverage.py` | 77 | 922 |
  | `tests/test_100_coverage_extra.py` | 58 | 784 |
  | `tests/test_100_final_coverage.py` | 41 | 953 |
  | `tests/test_100_final_push.py` | 52 | 846 |
  | `tests/test_coverage_boost.py` | 90 | 881 |
  | `tests/test_coverage_final.py` | 64 | 943 |
  | `tests/test_coverage_gaps.py` | 78 | 1335 |
  | `tests/test_full_coverage.py` | 155 | 2058 |
  | **subtotal** | **676** | **9811** |

  Per `test_100_coverage.py` line 32-34 the docstring is literally *"Push coverage to 100% — tests for every
  remaining uncovered line. Organized by module with line numbers in docstrings."* This is unambiguous
  coverage-padding: tests are organized by source-line, not by behavior. Recommend collapsing into the
  module-specific files (`test_claude_core.py`, `test_main.py`, etc.) and discarding redundant ones.

- `tests/test_security_full.py` is **4781 lines / 374 tests** — by far the largest file. A spot-check shows
  many trivial assertions (line 1068 has `except Exception: pass` inside the test itself). Likely also
  coverage-padded.

- `tests/conftest.py` (22 lines) restores `store` module-level state per *module*. Per-test pollution may
  still occur within a module.

- Tests embed dummy tokens via `os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:FAKE_TOKEN_FOR_TESTS")` —
  matches reasonable practice but means the real `telegram_bot.py` will be importable even when no token is
  set in CI.

- `tests/bench_telechat.py` (903 LOC) — not a unit test but collects under pytest. Consider moving to
  `benchmarks/` and excluding from default runs.

No collection-time import errors, but the test count is inflated and many tests appear to exercise the
codebase by mocking everything except the line under test.

---

## 8. Dependency / packaging issues

**[MEDIUM]** `requirements.txt` and `pyproject.toml` define **identical** core deps but `requirements.txt`
omits `aiohttp` — wait, actually it lists it. They're in sync at present. However:

- `requirements.txt` is essentially a duplicate of `dependencies` in `pyproject.toml`. Drop one (the
  pyproject form is canonical for a published wheel).
- Numerous optional deps are imported inside functions (`anthropic`, `claude_code_sdk`, `httpx`, `qrcode`,
  `PyMuPDF`, `python-docx`, `playwright`) but **none** are declared as `[project.optional-dependencies]` extras
  in `pyproject.toml`. Users discover requirements via runtime ImportError messages.

  **[RESOLVED 2026-06-03]** Extras now declared: `qr`, `sdk`, `docs`, `browser`, `httpx`, `mcp`, `dev`, `all` (`pyproject.toml:37-64`). `pip install telechatai[all]` works as the documented full install.

**[MEDIUM]** `pyproject.toml:25-32` — All deps use `>=` with no upper bound. `python-telegram-bot>=21.6`,
`anthropic>=0.99.0`, `slack-bolt>=1.21.0` — any major version bump (e.g. python-telegram-bot v22 broke API)
will break builds. Add `<NEXT_MAJOR` caps for libraries with frequent breaking releases.

**[MEDIUM]** `pyproject.toml:46` — package-data lists `["scripts/*", "*.html"]` but `scripts/` lives at the
**repo root**, not inside `telechat_pkg/`. The glob will not match and the install-time scripts won't ship in
the wheel. Verify the sdist with `python -m build --sdist && tar tf dist/*.tar.gz`.

**[RESOLVED 2026-06-03]** The non-matching `scripts/*` glob was removed; pyproject's package-data now lists only `["*.html"]` with an explanatory comment above it (`pyproject.toml:77-81`).

**[LOW]** `version = "1.1.5"` in pyproject vs `clientInfo: {"version": "1.6.0"}` hardcoded at
`mcp_client.py:120` — version drift.

**[RESOLVED 2026-06-03]** Both ends of the drift are fixed. Pyproject is now `1.2.0`. mcp_client.py no longer hardcodes a string — line 181 now calls `_telechat_version()` (single source of truth).

**[LOW]** `tests/` is not declared as a package and is not excluded — `setuptools.packages.find` `include`
already limits to `telechat_pkg*` so this is fine. ✓

---

## 9. Operational / config issues

**[HIGH]** `telechat_pkg/bot.db`, `telechat_pkg/bot.db-shm`, `telechat_pkg/bot.db-wal` exist inside the
**source tree**. `.gitignore` covers `*.db*` so they aren't committed (`git ls-files` confirms), but they are
present **inside the installable package directory**. That means:
- If anyone packages the project in CI without cleaning, the wheel will ship with this DB.
- More importantly, their existence implies `store.DB_PATH` once defaulted to the package directory. The
  current code (`store.py:20-36`) correctly resolves to `~/.telechat/bot.db`, so these files are leftover
  artifacts of an older version. Delete them.

**[HIGH]** `bot.err` at repo root is **1.1 MB** of repeated `Python: can't open file
'/Users/dev/projects/claudeplus/telechat/main.py': [Errno 2] No such file or directory`. Something — likely a
crontab, launchd plist, or `npm/` start script — is trying to invoke a non-existent `main.py` at the project
root. Find and fix the launcher, then truncate/delete `bot.err`. Confirmed `*.err` is in `.gitignore`.

**[MEDIUM]** `.env` is present in repo root. `.gitignore` covers it, but `git ls-files` should be re-verified
in any forked clone. The current `.env` contains a real `TELEGRAM_ALLOWED_USER_IDS=6775379103` — that's a
real Telegram user ID being leaked through this review (not secret, but identifies the operator).

**[MEDIUM]** Logging is configured **only** inside `_cmd_start` (`main.py:879-892`). Any module imported
before that (e.g. during `_cmd_init`) gets the root logger's default config (WARNING to stderr only). Tests
also bypass it. The handlers list `[_console, _file]` plus `basicConfig(handlers=...)` is fine, but
RotatingFileHandler writes to `bot.log` in cwd — works only because `_resolve_workdir` chdir'd to
`~/.telechat`. If chdir fails (line 50-52), logs land in the user's shell cwd.

**[MEDIUM]** No `.dockerignore`. `Dockerfile` exists (229 bytes) — likely COPYs `.git/`, `bot.db*`, `bot.err`,
`.env`, `.pytest_cache/`, etc. into the image. Verify.

**[MEDIUM]** `docker-compose.yml` is only 111 bytes — probably trivial. No `restart: unless-stopped`, no
healthcheck wired into the health server on `:8484`.

**[LOW]** `telechat_pkg/coder_projects.json` is *inside* the package — same anti-pattern as bot.db. It should
live in `~/.telechat/`. (`coder.py:36` resolves the canonical path correctly; the in-package one is stale.)

**[LOW]** `.coverage` (53 KB) committed in working tree (gitignored).

---

## 10. Suggested improvements

### P0 — fix or production will break

1. **Delete or rewrite `telechat_pkg/session_manager.py`.** Every method targets a non-existent `history`
   table and will raise on first call. Either remove the module or port it to the real `conversations` schema
   in `store.py`.
2. **Fix the WhatsApp file-browser sandbox escape** (`whatsapp_bot.py:430-435, 442, 455-473`). Validate that
   every resolved path is `.is_relative_to(BROWSE_ROOT)`. Today an attacker with a WhatsApp number can read
   arbitrary host files.
3. **Stop silently exposing the web chat on 0.0.0.0 with no token.** Either bind to `127.0.0.1` by default,
   or refuse to start when both `WEB_BIND != localhost` and `WEB_CHAT_TOKEN` is empty.
4. **Stop trusting `X-Forwarded-For` blindly** (`web_chat.py:53-59`). Only honor it when an explicit
   `WEB_CHAT_TRUST_XFF=true` is set; otherwise use `peername` only.
5. **Replace `os._exit(0)` on SIGINT** (`main.py:1011, 1032`) with a graceful shutdown that flushes the DB
   writer queue and awaits `runner.cleanup()`.
6. **Remove hardcoded `permission_mode="bypassPermissions"`** from `claude_core.py:425`. Default to `auto`
   and respect the user setting.
7. **Delete `telechat_pkg/bot.db*`, `bot.err`, `telechat_pkg/coder_projects.json`** from working tree.

### P1 — significant quality / correctness

8. Add a global `threading.Lock` (or per-key locks) to `store.SessionManager._cache`/`_active` and to
   `_rate_state` in `check_rate_limit`. Today these mutate across worker threads + asyncio.
9. Use `INSERT INTO ... VALUES (?, ?, ?, ?, ?)` for `save_turn` and **commit as a single transaction** instead
   of three separate `_enqueue_write` calls. Drop the `OR IGNORE` and use a monotonic counter for `ts`.
10. Bound `_handle_chat` concurrency per WS connection. Store created tasks in a set; cancel them on disconnect;
    cap to e.g. 1 in-flight per client (the Claude call already takes the user's full turn).
11. Make `MemoryStore` reuse `store._get_conn()` to avoid two pools on one DB file and to consolidate WAL
    pragmas.
12. Validate `MCP_CONFIG_FILE` contents: enforce an allow-list of commands or require an explicit
    `MCP_ALLOW_EXEC=true` env flag with a startup warning.

    **[PARTIALLY RESOLVED 2026-06-03]** `MCP_ALLOWED_COMMANDS` allowlist exists (`mcp_client.py:41-65`) and `add_server()` refuses to register a server whose command isn't on it. **Remaining gap** (tracked in ticket 0019): env passed to the subprocess is still `{**os.environ, **server.env}` with no scrubbing, and the allowlist matches on command *basename* only — a poisoned PATH or planted binary redirects `npx`/`python3` to attacker code. Tests in `tests/test_mcp_client.py` (committed by other agent) pin the *current unsafe* behavior; the safe behavior is the bug-fix delta tracked by 0019.
13. Tighten the `pgrep -f telechat_pkg.main` shutdown (`main.py:930`) to match exact process names or to
    check UID. Prefer a PID file in `~/.telechat/telechat.pid`.
14. Split `telegram_bot.py` (3527 lines) into `telegram/{commands,settings,browser,memory,sessions,media}.py`.
    The single file blocks parallel development and review.
15. Collapse the nine `test_100_*` / `test_coverage_*` / `test_full_coverage.py` test files (~9800 LOC, 676
    tests) into behavior-focused tests inside the per-module test files. Audit each retained test for whether
    it asserts behavior or just touches a line.
16. Declare optional deps as `[project.optional-dependencies]` (extras: `api`, `sdk`, `voice`, `images`,
    `docs`, `browser`, `qr`) so `pip install telechatai[all]` becomes the documented "full" install.

### P2 — polish / hygiene

17. Add upper bounds to `pyproject.toml` deps (`python-telegram-bot>=21.6,<23`, etc.).
18. Replace 80+ `except Exception:` blocks with targeted exception types or at minimum `log.exception(...)`
    so failures are visible.
19. Move the hand-rolled QR encoder (`main.py:637-846`) out of `main.py` into `qr_util.py`, or drop it and
    require `qrcode` (already optional).

    **[RESOLVED 2026-06-03 by ticket 0017]** Hand-rolled QR + Reed–Solomon was already removed before this branch (qr_util.py docstring). The remaining ~60-line tail of orphan helper duplicates in main.py was deleted in ticket 0017 (commit `828f934`). Closes both halves of #19.
20. Fix `Optional[callable]` type hints in `claude_core.py` to `Optional[Callable[..., Awaitable[None]]]`.
21. Add a `pyproject.toml` `[tool.ruff]` / `[tool.mypy]` block and run in CI. Current code passes neither
    cleanly.
22. Add `[tool.pytest.ini_options]` with `addopts = --strict-markers -ra` and exclude `tests/bench_telechat.py`
    from the default run.
23. Cache the web UI HTML in memory at startup instead of reading every request (`web_chat.py:115`).
24. Bound `_auth_failures` (e.g. LRU 1000 entries) in `web_chat.py`.
25. Document the data home (`~/.telechat/`) clearly in README; explain interactions between the npm CLI and
    the pip-installed CLI sharing `~/.telechat/config.json`.
26. Verify `Dockerfile` / `docker-compose.yml` don't ship `.env`, `bot.db*`, or `.git/` into images. Add a
    `.dockerignore`.
27. Reconcile version: `pyproject.toml` says `1.1.5`, `mcp_client.py:120` says `"1.6.0"`. Centralize in a
    `__version__` constant.

---

*End of review.*
