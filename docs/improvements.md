# Telechat — Improvement Suggestions

Reviewed: 2026-07-29. Scope: `telechat` (telechatai 1.2.0, `main` @ `765fcba`), plus
`../telechat-features` and `../telechat-website` (the `telechat.fyi` landing page).

`../telechat-features` is not a separate codebase or a clone: it is a **git worktree of
this same repository** (`.git` → `telechat/.git/worktrees/telechat-features`) checked out
on `feat/self-improving-tickets` @ `92efe4d`, a commit that is already an ancestor of
`main`. It is a stale duplicate — still carrying `CODE_REVIEW.md` at the root from before
ticket 0022 moved it into `docs/` — plus a `venv/`. See item 27.

> **Scope note:** items **1–27** are product, packaging, and process. A
> [second pass](#second-pass--verified-source-level-findings) later the same day added items
> **28–35**, which *do* cover source-level correctness — because `CODE_REVIEW.md` proved too
> stale to defer to. Those items carry measured coverage figures and a
> [disposition table](#appendix-a--code_reviewmd-findings-verified-fixed) for the old review.
> See the [revised sequencing](#revised-sequencing-for-these-items) if you are picking up work.

Items 1–27 are **product, packaging, and process** oriented and deliberately do
not re-litigate the source-level bug findings already catalogued in
[`docs/CODE_REVIEW.md`](CODE_REVIEW.md) — most of that review's P0 list has since been
fixed (web chat now binds `127.0.0.1` and refuses to start exposed without a token,
XFF is gated behind `WEB_CHAT_TRUST_PROXY`, the WhatsApp browse sandbox uses
`is_relative_to`, `session_manager.py` creates its schema, the SDK no longer hardcodes
`bypassPermissions`). Where a finding below overlaps, it is cross-referenced.

Effort estimates assume one engineer (or agent) familiar with the tree, including tests and
docs. **S** = up to a day, **M** = 2–4 days, **L** = 5+ days. Each item states its own
figure where it differs from the band.

---

## What the app is

A self-hosted personal bot that proxies messages to Claude — either the `claude` Code
CLI (free with a subscription) or the Anthropic API — and exposes it over Telegram,
WhatsApp (Green API), Slack (Socket Mode), and a local aiohttp web chat, all from one
process selected by `BOT_MODE`. Around it sits a large feature surface: multi-session
conversations, FTS5 memory, a knowledge base, cost budgets, smart model routing, media
generation, MCP, a watchdog, and a self-improving loop (LLM-judge, preference learning,
prompt A/B).

The strongest and least-advertised part is the **Claude Desktop bridge**: hooks in
`~/.claude/settings.json` push Stop/Notification events to Telegram as AI-triaged cards,
and replies are injected back into the live session via `claude --resume`, optionally
gating every Bash/Write/Edit behind a phone approval. That is a genuinely differentiated
capability, and all five unclaimed tickets in `agents/inbox/` extend it.

~52k lines of Python: 21k app code in `telechat_pkg/`, 31k tests in `tests/`.

---

## P0 — Broken or actively decaying

### 1. The Dockerfile cannot build — Docker is a documented, dead path

**S (~30 min).** `Dockerfile` copies from the repository root:

```dockerfile
COPY main.py claude_core.py telegram_bot.py whatsapp_bot.py slack_bot.py bot.py ./
CMD ["python", "-u", "main.py"]
```

There are no `.py` files in the repository root — they all live in `telechat_pkg/` — and
`bot.py` does not exist anywhere in the tree. `docker compose up -d` fails at build time.
The README documents Docker as a supported install path ("Docker (API mode only)") and
`docker-compose.yml` ships alongside it, so every user who follows that path hits a wall.

This is a layout regression: the file predates the move into `telechat_pkg/` and was never
updated. The fix is to install the package properly rather than copy loose modules:

```dockerfile
COPY pyproject.toml README.md MANIFEST.in ./
COPY telechat_pkg/ ./telechat_pkg/
RUN pip install --no-cache-dir .
CMD ["telechat", "start"]
```

While in there: the image needs the API-mode extras it actually uses (`httpx`, `docs`),
should run as a non-root user, and should declare a `HEALTHCHECK` against the existing
`:8484/health` endpoint. Add a `docker build` step to CI so this cannot rot again.

> **Done (2026-07-30).** Installs the package rather than copying a file list, adds the
> `httpx`/`docs` extras, a non-root user, and the `HEALTHCHECK`. `docker-compose.yml` gains a
> named volume for `/data` (a rebuild used to take the database with it), pins
> `CLAUDE_MODE=api`, and publishes the unauthenticated health port on loopback only. A CI job
> builds the image, runs `telechat --version` inside it, and validates the compose file.

*Note: `CODE_REVIEW.md` #26 asked only whether the Dockerfile leaks `.env`/`bot.db`. It
does not — `.dockerignore` was added in ticket 0021 — but the build itself is broken,
which that review did not catch.*

### 2. CI never runs the pytest suite

**S–M (2h, more if it surfaces failures).** `AGENTS.md` states "3000+ tests, ~99%
coverage" and rule 8 requires `pytest -q` before moving a ticket to `done/`. That is
entirely honour-system: `.github/workflows/e2e.yml` runs `bash tests/run-all.sh`, which
runs three shell scripts (`e2e-install.sh`, `e2e-platforms.sh`, `e2e-features.sh`) and
nothing else. `grep -rn pytest .github/ tests/*.sh scripts/*.sh` returns nothing. The
PyPI publish workflow gates on the same shell suite.

So the 31k lines of Python tests — the single largest quality investment in this
repository — are never executed by any automated system. Worse, the shell layers are
self-skipping by design (they skip when platforms are unconfigured, and the destructive
lifecycle test is opt-in behind `TELECHAT_E2E_LIFECYCLE=1`), so a green CI run can mean
almost nothing was checked.

Add a `pytest` job: `pip install -e ".[dev,all]"` then `pytest -q --cov=telechat_pkg`,
across the Python versions `pyproject.toml` advertises (3.9–3.13). Gate the publish
workflow on it. Consider a coverage floor so the claimed number becomes an enforced one.

Related: `pyproject.toml` declares `requires-python = ">=3.9"`, but
`whatsapp_bot.py:75` carries the comment *"we target 3.10+ per pyproject"*. One of the two
is wrong. A test matrix would have caught the disagreement.

> **Done (2026-07-30).** `.github/workflows/pytest.yml` runs the suite across the
> advertised versions plus a coverage job with an enforced 80% floor (measured: **84.32%**),
> and `publish-pypi.yml` gates on a pytest job.
>
> The matrix settled the 3.9/3.10 disagreement immediately, and in favour of the
> *comment*: on 3.9 the suite fails and then hangs, and `doctor.py:69` requires 3.10+ and
> reports a failing check there — so the project's own health check contradicted its
> declared floor. `requires-python` is now `>=3.10`, the 3.9 classifier is gone, and the
> npm wrapper's "Python 3.9+" messages and its version gate were raised to match.
> A separate find along the way: `claude-code-sdk` requires 3.10+, so
> `pip install telechatai[all]` could not resolve at all on the 3.9 the metadata
> advertised — moot now that the floor is 3.10.

### 3. Default model IDs are deprecated and past their announced retirement

**S–M (~half a day).** The API-mode defaults point at models that are no longer current:

| Location | Default | Status |
|---|---|---|
| `claude_core.py:68` | `claude-sonnet-4-20250514` | Deprecated; retirement announced for 2026-06-15 |
| `smart_router.py:114` | `claude-sonnet-4-20250514` | same |
| `smart_router.py:115` | `claude-opus-4-20250514` | Deprecated; retirement announced for 2026-06-15 |
| `two_agent.py:29` | `claude-sonnet-4-20250514` | same |
| `.env.example:20` / README | `claude-sonnet-4-20250514` | same |

That announced date has already passed. Whether or not the endpoints have been switched
off yet, shipping a default that Anthropic has scheduled for removal means a fresh
`CLAUDE_MODE=api` install is one deprecation sweep away from returning 404 on every
message — with no fallback path in the code.

Current IDs are `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` (the Haiku
defaults in `evaluator.py`, `memory.py`, `two_agent.py`, and `smart_router.py` are
already current and need no change). Note the newer models drop `temperature`/`top_p`
and reject `thinking.budget_tokens`, so check the API call sites before swapping.

Two structural fixes worth doing at the same time:

- **Centralise the IDs.** They are currently hardcoded across five modules. One
  `models.py` with named tiers, overridable by env, makes the next bump a one-line change.
- **Add a startup model check.** `doctor.py` already exists; have it call `GET /v1/models`
  in API mode and warn when a configured model is absent from the list. That converts a
  future silent 404 into an actionable boot warning.

> **Done (2026-07-30), except the startup model check.** `telechat_pkg/models.py` is the
> single source for both IDs and pricing; defaults are `claude-opus-5` / `claude-sonnet-5` /
> `claude-haiku-4-5`, each overridable via `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU`, and
> the five modules that hardcoded IDs now read from it. No call site set `temperature`,
> `top_p`, or `thinking.budget_tokens`, so the swap needed no other change. A test fails if
> any shipped tier points at a model on the retired list. **Still open:** the `doctor.py`
> `GET /v1/models` startup check.

### 4. Three different version numbers ship in one release

**S (~1h).** Three sources of truth, all disagreeing today:

| File | Version |
|---|---|
| `pyproject.toml` | `1.2.0` |
| `telechat_pkg/__init__.py` | `1.1.5` |
| `npm/package.json` | `1.1.1` |

The consequences are concrete, not cosmetic. `updater.current_version()` reads
`importlib.metadata.version("telechatai")` → `1.2.0`, while `mcp_client.py:101` reports
`__version__` → `1.1.5`, and `telechat --version` from the npm wrapper prints
`1.1.1`. The updater then compares its number against *both* the PyPI and npm registries
(`updater.py:156-161`) and nags when either is higher — so an npm-installed user on the
newest release is told to update, permanently, because the wrapper's version is two
minors behind the Python package it launches.

Make `pyproject.toml` (or `__init__.py`) the single source, derive the other two in
`scripts/publish.sh`, and add a CI check that fails when they diverge.
*(`CODE_REVIEW.md` #27 flagged the pyproject/mcp_client half of this; the npm wrapper is
the third leg and the one that reaches users.)*

> **Done (2026-07-30).** `pyproject.toml` is the single source. `__init__.py` resolves the
> version from installed metadata (what the updater reads) with a pyproject fallback for
> source checkouts; `npm/package.json` is derived by `scripts/sync-version.sh`, which
> `publish.sh` runs and CI checks with `--check`. All three now report 1.2.0.

### 16. In API mode every turn is recorded as costing $0 — `/budget` silently enforces nothing

**S (~1 day).** Cost is persisted from whatever the backend returned:

```python
# telegram_bot.py:2837
cc.track_cost(PLATFORM, str(uid), stats.get("input_tokens", 0),
              stats.get("output_tokens", 0), stats.get("cost_usd", 0))
```

Only the CLI path ever populates `cost_usd` (`claude_core.py:551`, from the stream's
`total_cost_usd`). `ask_claude_api` and `ask_claude_api_async` return token counts and no
cost at all (`claude_core.py:341-345, 386-387`), so with `CLAUDE_MODE=api`:

- `cost_tracking.cost_usd` accumulates `0.0` forever;
- `BudgetManager.check_budget` sums that column (`cost_budget.py:118, 133`), so daily and
  monthly caps **never trip** — `/budget` reports `$0.00 / $5.00` no matter how much the
  user spends;
- `/usage` shows tokens but a false cost;
- `web_chat.py:406` only calls `track_cost` when `cost_usd` is truthy, so API-mode web
  turns are not recorded at all.

A spend guard that silently does nothing is worse than no guard — a user who sets a
budget reasonably believes they are protected. Add a pricing table (input / output /
cache-read rates per model), compute the cost when the backend does not supply one, and
label computed figures as estimates in `/usage`. This pairs naturally with the `models.py`
registry from item 3 — same table, same bump cadence.

> **Done (2026-07-30).** The pricing table lives in `telechat_pkg/models.py` alongside the
> tiers, keyed by longest-prefix match so dated snapshots resolve via their family. An
> unknown model estimates at Sonnet rates and logs once rather than returning 0.0 — for a
> spend guard, erring high is the safe direction. Estimates are labelled: `stats` carry
> `cost_estimated`, the per-turn footer prefixes `~`, and `/budget` says so in API mode. The
> `web_chat.py` gate on a truthy `cost_usd` is fixed too, so API-mode web turns are recorded
> at all.

### 17. The Desktop bridge has zero tests, and the tickets queued against it assume otherwise

**M (3–4 days).** `grep -rl desktop_bridge tests/` returns **nothing**: 1,817 lines
implementing the project's differentiator — hook handling, approval decisions, transcript
parsing, `claude --resume` injection, Telegram transport — are covered by no test at all.
Meanwhile all five inbox tickets (0023–0027) list `tests/test_desktop_bridge.py` in their
`touches:` frontmatter, and that file does not exist. Whoever claims the first ticket
inherits the whole harness as unscoped work.

Build the harness before the features, alongside the split in items 6/14: a fake
`~/.claude/projects` transcript tree, a stubbed `_tg_call`, a fake `claude` binary for the
resume path, and an in-memory bridge DB. Cover transcript parsing, digest formatting and
its no-summarizer fallback, the approve/deny hook round-trip, short-session-id resolution,
and text routing — then land 0023/0024/0026 against it.

This is also where the "~99% coverage" claim in `AGENTS.md` breaks down: coverage is high
across the older modules and absent on the newest and most complex one.

> **Done (2026-07-30) — the harness, not the split.** `tests/test_desktop_bridge.py` is 71
> tests on the four fakes this item asks for (fake `~/.claude/projects` tree, stubbed
> `_tg_call`, fake `claude` binary, isolated bridge DB), covering transcript parsing, digest
> formatting and its no-summarizer fallback, the approve/deny round-trip including the
> documented five-minute fail-open, short-id resolution, and text routing.
> `desktop_bridge.py` 19% → 45%; total 84% → 87%, with the CI floor raised to 85.
> **Still open:** splitting the module (items 6/14) before tickets 0023–0027 land.

---

## P1 — Significant quality, maintainability, or reach

### 5. No linter, formatter, or type checker is configured

**S to add, M–L to clean up.** `pyproject.toml` has no `[tool.ruff]`, `[tool.mypy]`,
`[tool.black]`, or `[tool.pytest.ini_options]` block — `grep` for any of them returns
nothing. For a 52k-line codebase worked on by multiple agents in parallel, that is the
highest-leverage missing guardrail: style drift and easy type errors are currently caught
only by human review, and `AGENTS.md` gives agents no mechanical standard to conform to.

Add `ruff` (lint + format) and run it in CI. Add `mypy` in non-strict mode over
`telechat_pkg/` with a per-module ignore list, then burn the list down over time. Add
`[tool.pytest.ini_options]` with `addopts = "--strict-markers -ra"` and exclude
`tests/bench_telechat.py` from the default run. Wire all three into a pre-commit config
so parallel agents self-check before opening work.

### 6. `telegram_bot.py` is a 3,774-line monolith with 63 bare exception handlers

**L (3–5 days).** It is the largest file in the project by a wide margin, holds 96
top-level definitions, and contains `except Exception` 63 times (the next worst is
`desktop_bridge.py` at 30, itself now 1,817 lines and growing — all five inbox tickets
target it).

Two costs compound here. First, it is the main serialisation point for the parallel-agent
workflow `AGENTS.md` describes: two agents touching different Telegram features still
collide on one file, and `agents/check-overlap.sh` will keep telling them so. Second, the
blanket handlers mean genuine failures surface as a generic "something went wrong" in
chat, with no traceback in the log unless the operator happens to be running `--debug`.

Split along the seams already visible in the command groups the README documents:
`telegram/{commands,settings,sessions,memory,media,browser,bridge}.py`, with the
`Application` wiring left in `telegram_bot.py`. Do the same for `desktop_bridge.py`
*before* the five bridge tickets land, not after. Separately, sweep the handlers: keep
the catch at the top-level dispatch boundary (a user-facing bot should not die on one bad
message) but make every one of them `log.exception(...)`, and narrow the inner ones to
the exceptions actually expected.

### 7. Dependency declarations are duplicated and unbounded

**S (~1h).** `requirements.txt` restates the six core dependencies from
`pyproject.toml` verbatim. Two files, no mechanism to keep them in sync — and the
Dockerfile (item 1) builds from the `requirements.txt` copy, so a dependency added to
`pyproject.toml` alone would silently not reach the image. Delete `requirements.txt` and
have everything install the package, or generate it from `pyproject.toml` in the release
script.

Separately, every dependency is lower-bounded only (`python-telegram-bot>=21.6`,
`anthropic>=0.99.0`, `aiohttp>=3.9.0`, …). A major release of any of them breaks fresh
installs for everyone with no warning — this has already happened once in this tree
(commit `0829719`, "fix cassette replay under aiohttp 3.14"). Add upper bounds on the
libraries whose APIs you actually touch. *(Also `CODE_REVIEW.md` #17.)*

### 8. The website undersells the product

**M (1–2 days).** `../telechat-website/index.html` is a single 140-line static page: a
title, a one-line tagline, the install command, two links, and a QR code. It is the whole
of `telechat.fyi`.

For a self-hosted tool whose entire distribution is "someone reads about it and runs
`npm install -g telechat`", that page is the funnel — and it currently communicates none
of the things that would make someone install: no screenshot of the Telegram UI, no
mention of the Desktop bridge (the actual differentiator), no feature list, no platform
comparison, no link to the setup docs, no explanation that CLI mode is free with a
Claude subscription.

The material already exists in the README. The work is presentation, not authoring: add
a hero screenshot of a bridge triage card, a short feature grid, the three-platform
comparison table, and a "how it works" diagram (`docs/architecture.html` may be
reusable). Keep the single-file, no-build structure — it is a virtue.

### 9. The Desktop bridge is the differentiator and it is buried

**S (~2h).** The bridge does not appear until line 339 of a 523-line README, below
three platform setup walkthroughs, and is absent from the tagline, the feature list
ordering, and the website entirely. Yet it is the one capability no other Claude-on-
Telegram bot has, and it is where all current development is pointed.

Lead with it: mention it in the README's opening paragraph, move its section above the
per-platform setup guides, and give it a screenshot. This is pure positioning work with
no code change, and it makes items 8 and 10 easier.

### 10. Split the 523-line README

**S (~3h).** It currently holds the pitch, three platform setup walkthroughs, the full
env-var reference, the command reference (60+ commands across five tables), the bridge
manual, the project structure, and troubleshooting. Nobody reads all of it, and the
information a prospective user needs (what is this, why would I want it) is interleaved
with information only an existing user needs (what does `/exportmem` do).

Move to `docs/`: `setup-telegram.md`, `setup-whatsapp.md`, `setup-slack.md`,
`commands.md`, `configuration.md`, `bridge.md`. Leave the README as pitch → install →
quickstart → links. `docs/` already exists and is already the documented home for this
kind of material per `AGENTS.md`.

### 18. Telegram is the product; the other three adapters are demos

**L (6–8 days).** Command counts per adapter, from the handler registrations:

| Adapter | Commands | Notable gaps |
|---|---:|---|
| Telegram | 55 | — |
| WhatsApp | 28 (`!`-prefixed) | no `/code`, `/kb`, `/plan`, `/schedule`, media gen, budgets |
| Slack | 14 | no memory beyond `/memories`, no sessions beyond list/pin, no tools at all |
| Web chat | ~0 | chat only; no commands surface in the UI |

Every feature is hand-written per adapter, so parity decays with each addition and the
README's platform-comparison table needs manual upkeep to stay honest. The fix is a
platform-agnostic command registry: each command declares name, args, help text, required
capabilities (buttons / files / voice / streaming) and a handler taking a normalised `Ctx`
(platform, user, session, reply/edit/upload callbacks). Adapters become renderers —
inline keyboards on Telegram, Block Kit on Slack, numbered `!` commands on WhatsApp, UI
controls on web.

Do the `telegram_bot.py` split (item 6) as step one of this rather than as separate churn,
and generate `/help` and the docs command tables (item 10) from the registry. Ship the
registry plus Slack and web parity for the top ~20 commands first; WhatsApp follows.

### 19. `docs/architecture.md` documents a program that does not exist

**S (~half a day).** `AGENTS.md` sends every agent to `docs/architecture.md` for the
architecture overview. That file describes `src/main.py`, `src/bot/core.py`,
`src/bot/orchestrator.py`, `src/claude/facade.py`, FastAPI, APScheduler, Pydantic
settings, a YAML project registry, and tables like `project_threads` and `webhook_events`
— **none of which exist in this repository**. There is no `src/` directory; the real
entry point is `telechat_pkg/main.py` and persistence is `store.py`. It also claims
version 1.6.0 and cites "source files reviewed" that are absent.

For a tree worked by parallel agents told to trust it, a canonical doc describing a
different codebase is worse than no doc. Rewrite it from the actual modules (the component
map in `CODE_REVIEW.md` §1 is accurate and a good starting point), or delete it and point
`AGENTS.md` at that section. `docs/architecture.html` is presumably generated from the
same fiction — check before reusing it for the website (item 8).

While in the docs: the README never mentions `BOT_MODE=web`, even though `main.py:14`
supports it, the `init` wizard offers it as option 4, and `web_chat.py` implements a full
UI with token auth. A shipped, wizard-promoted platform is missing from the platform
table, the setup section, and the feature list.

### 20. ~60 environment variables are read by the code and documented nowhere

**M (~2 days).** `.env.example` documents 61 variables. Comparing it against every
`os.getenv`/`os.environ` key in `telechat_pkg/` turns up roughly the same number again
that are undocumented, including whole feature groups:

`KB_*` (5), `SMART_ROUTE_*` (6), `TWO_AGENT_*` (3), `JUDGE_*` (2), `PROMPT_*` (3),
`MCP_*` (4), `WEB_CHAT_*` (6), `COST_*` (4), `UPDATE_*` (4), `BROWSER_*` (3),
`AUTO_MEMORY*` (2), `EXTRACT_*`, `HEALTH_PORT`, `HEALTH_BIND_ADDR`, `TELECHAT_HOME`,
`TELECHAT_DEBUG`, `MAX_CONCURRENT_TASKS`, `PLANNER_MODEL`, `EXECUTOR_MODEL`, `DB_PATH`…

So features that are implemented, tested, and tracked as **Done** are undiscoverable —
including security-relevant ones (`WEB_CHAT_BIND`, `WEB_CHAT_TRUST_PROXY`,
`MCP_ALLOW_ANY_COMMAND`). Declare each setting once as data (name, default, type, feature
group, secret?, description) and generate from it: `.env.example`, a
`docs/configuration.md` table (item 10), and a `telechat config` command that prints the
effective configuration with secrets masked and each value's source. Have `doctor.py`
validate against the same spec and flag unknown keys, which catches typos that currently
fail silently.

### 21. No observability beyond a health endpoint

**M (2–3 days).** `docs/implementation-tracker.md` lists `telemetry.py` under Phase 3 as
Planned; there is no metrics surface today. `health.py` exposes component status and the
circuit-breaker state, and logs go to a rotating `bot.log` in free-text form.

When a user reports "the bot got slow", nothing in the system can distinguish Claude
latency from queueing, rate limiting, subprocess startup, or SQLite write-queue
back-pressure. Add structured (JSON) logging with a per-turn correlation id threaded
through the adapter → `claude_core` → store path, a `/metrics` endpoint on the existing
health server (turn latency by platform/model/backend, tokens, cost, tool-call counts,
error classes from `error_classifier.py`, breaker transitions, write-queue depth), and
have `/stats` render the same data in-chat. The self-improving loop already collects
quality signal; this is the operational half it lacks.

### 22. Duplicate subsystems doing the same job

**M (2–3 days).** Three pairs, each a bug that fires on only one of the two paths:

- **Sessions:** `store.SessionManager` (authoritative, `_lock`-protected, backs the
  adapters) and `session_manager.SessionBrowser` (own schema bootstrap, own legacy-history
  migration, wired into `telegram_bot.py:2949`). Two writers, one DB.
- **Scheduling:** `scheduled_tasks.py` (asyncio cron loop) and `auto_scheduler.py`
  (natural-language scheduling, whose docstring says it "extends" the former). Plus
  `commitments.py` scheduling proactive reminders on its own.
- **URL handling:** `web_fetch.py`, `web_search.py`, and `link_understanding.py` overlap
  on fetch-and-summarise.

Consolidate to one owner each: `store` owns session state and `SessionBrowser` becomes a
read-only view; one timer loop with `auto_scheduler` and `commitments` as producers;
`link_understanding` delegating to `web_fetch`. Two module docstrings also lost their
first sentence to an earlier cleanup pass (`scheduled_tasks.py:3`, `doctor.py:3`) — cheap
to fix while in there.

### 23. Message reactions as implicit feedback — the consumers already exist

**S (~1 day).** `feedback.py`, `evaluator.py`, and `preferences.py` all consume ratings,
and `docs/advanced-telegram-features.md` §4 designed exactly this — but nothing collects
ratings passively: there is no `MessageReactionHandler` anywhere in the package, so signal
arrives only when a user types `/rate`. Map 👍/❤️ → 5, 👎 → 1, 🔥 → append to
`learnings.md`, 🤔 → flag for review. One handler, zero typing for the user, and every
downstream consumer is already built and tested. The cheapest available increase in
training signal for the self-improving loop.

---

## P2 — Polish and hygiene

### 11. `os._exit(1)` in the startup path

**S (~1h).** `main.py:791` still hard-exits, skipping `atexit` hooks, the SQLite writer
queue flush, and `runner.cleanup()`. The SIGINT handler was fixed (the review's
`main.py:1011/1032` are gone), so this is the last one — but it is on the startup-failure
path, where an aborted boot can now strand queued writes. Replace with a raised
`SystemExit` and a `finally` that drains the writer.
*(Remainder of `CODE_REVIEW.md` #5.)*

### 12. No dev-environment bootstrap documented

**S (~1h).** `AGENTS.md` rule 8 says "run `pytest -q`", but the default `python3` on a
current macOS/Homebrew box is 3.14 with no pytest, and nothing in `AGENTS.md`, the README,
or `scripts/` tells a new contributor (or agent) how to get a working environment.
`../telechat-features` has a `venv/`, this tree does not. Document the three lines —
`python3.12 -m venv venv && source venv/bin/activate && pip install -e ".[dev,all]"` —
in `AGENTS.md`, or add `scripts/dev-setup.sh`.

### 13. Missing repository conventions

**S (~2h).** No `CHANGELOG.md` (with `telechat update` shipping in-product, users have
no way to see what an update changes), no `CONTRIBUTING.md`, no `SECURITY.md` (relevant —
this is a bot that can execute shell commands via the Claude CLI), no GitHub issue or PR
templates. The `agents/` ticket system is excellent for AI agents and invisible to human
contributors; a short `CONTRIBUTING.md` pointing at it would bridge that.

### 14. Bridge hardening before the inbox tickets land

**M, folded into the ticket work.** The five queued tickets (0023–0027: live streaming,
interrupt, follow-up actions, granular approval, voice/slash passthrough) all add
surface to `desktop_bridge.py`. Two things are worth doing first, since they get harder
with each ticket:

- **Split the module** (see item 6) — five tickets landing serially in one 1,817-line
  file will serialise the work and make review painful.
- **Close the approval-timeout gap.** The README documents that the `PreToolUse`
  approval hook times out after five minutes and *falls through to the normal permission
  flow*. Fail-open is a defensible default for a personal tool, but it should be a
  documented, configurable choice (`BRIDGE_APPROVAL_TIMEOUT_ACTION=allow|deny`) rather
  than an implicit one — especially as ticket 0026 makes approval more granular and
  therefore more likely to be relied upon.

### 15. `agents/` history is a real asset — surface it

**S (~1h).** 23 completed tickets in `agents/done/`, each with an outcome summary and
test evidence, plus two ADRs. That is a better engineering record than most projects this
size have, and it is invisible outside the repository. A short "how this project is
built" section — the ticket lifecycle, the `touches:` overlap check, the multi-agent
coordination model — would be genuinely interesting content for the README or website,
and doubles as the contributor onboarding from item 13.

### 24. Telegram Mini App reusing the existing web UI

**M (~3 days).** `web_chat_ui.html` is already a complete chat UI — markdown rendering,
stats, session handling, token auth. Serving it as a Telegram Web App (a `WebAppInfo`
button plus `initData` HMAC validation against the bot token) gives users scrollback, rich
rendering, file drops, and session switching *inside* Telegram, without maintaining a
second frontend. Highest ratio of perceived polish to new code on this list, and it makes
the web adapter's parity gap (item 18) matter less.

### 25. The bridge assumes one host and one platform

**M (3–4 days).** Bridge cards go out through `_tg_call` — raw Telegram HTTP, hardcoded
(`desktop_bridge.py:265`) — and session state (`bridge_state`, `bridge_follows`) carries no
host identity, so a laptop and a desktop both reporting into the same bot produce
indistinguishable cards and ambiguous short-id resolution. Add a `host` label to session
records and cards, and route bridge output through the shared command core (item 18) so
Slack and web can receive cards and replies too. Natural follow-on to items 14 and 18.

### 26. No per-user isolation for multi-person deployments

**M (~3 days).** Access control is a flat allowlist per platform
(`TELEGRAM_ALLOWED_USER_IDS`, `WHATSAPP_ALLOWED_NUMBERS`, `SLACK_ALLOWED_USER_IDS`), and
everyone who passes it shares one Claude auth, one `CLAUDE_CLI_WORK_DIR`, and one
permission mode — which `/permissions` lets any allowed user change, up to
`bypassPermissions`. Conversations and memory are keyed per user, but capability is not.

That is defensible for the documented single-operator use case; it is not obvious to
someone adding a second Telegram ID. Two pieces of work: a short "threat model / who
should run this" section in the README stating the trust boundary plainly (cheap, do it
with item 9), and — if multi-user is a goal — per-user working directories, a per-user
permission ceiling the user cannot raise, and an auditable tool-action log.

### 27. Remove the `telechat-features` worktree

**S (~15 min).** Its branch `feat/self-improving-tickets` @ `92efe4d` is already an
ancestor of `main`, so the checkout holds nothing unique — only stale copies (pre-0022
`CODE_REVIEW.md` at the root) and a `venv/`. Two live checkouts of one repository sitting
side by side is a real hazard for a tree that multiple agents edit: work done in the wrong
directory looks committed and never reaches `main`. `git worktree remove
../telechat-features` and delete the branch; keep the `venv/` recipe as item 12 instead.

---

## Suggested sequence

1. **Week 1 — stop the bleeding.** Items 1–4, 16, 27 (Dockerfile, pytest in CI, model IDs,
   version unification, API-mode cost accounting, drop the stale worktree). All small, all
   currently user-visible or actively misleading, and item 2 protects everything after it.
2. **Week 2 — guardrails.** Items 5 and 7 (ruff/mypy/pytest config, dependency
   declarations), then items 17 + 6/14 together: the bridge test harness and the
   `desktop_bridge.py` split, both ahead of tickets 0023–0027.
3. **Week 3 — bridge interactivity.** Tickets 0023 (live streaming), 0024 (stop button),
   0026 (granular approval), on the harness from week 2. This is where the differentiator
   stops feeling like a notification feed.
4. **Week 4 — reach and truth.** Items 8–10, 19 (website, bridge positioning, README
   split, architecture doc rewrite + `BOT_MODE=web`). These convert existing engineering
   into users, and stop the canonical docs from misleading the agents working the tree.
5. **Then — depth.** Item 18's command registry (with item 6's `telegram_bot.py` split as
   its first step), then 20 (config as data), 21 (observability), 22 (deduplication),
   23 (reaction feedback). Item 18 is the load-bearing one: until it lands, every new
   feature is written three or four times and verified by hand.
6. **Ongoing.** The exception sweep from item 6 alongside normal ticket work; remaining P2
   items as they fit.

Rough totals: ~6 days for wave 1, ~7 for wave 2, ~4 for wave 3, ~5 for wave 4, ~16 for
wave 5.

---

## Mapping to existing tickets and review findings

| This doc | Existing | Relationship |
|---|---|---|
| 17, 14 | `agents/inbox/0023–0027` | Prerequisite — build the harness and split the module first |
| — | `agents/inbox/0025` | Keep as queued; lands naturally after 0023 |
| 6, 18 | `CODE_REVIEW.md` #14 | The `telegram_bot.py` split is step one of the command registry |
| 16 | — | Not in the review; found in this pass |
| 19, 20 | `CODE_REVIEW.md` #25 | Superset — generated config reference, plus the architecture doc |
| 7 | `CODE_REVIEW.md` #17 | Same finding, plus the 3.9/3.10 disagreement |
| 11 | `CODE_REVIEW.md` #5 | Remaining half — the startup-path `os._exit` |
| 4 | `CODE_REVIEW.md` #27 | Third leg (npm wrapper), the one users see |
| 5 | `CODE_REVIEW.md` #21, #22 | Same, wired into CI |

`CODE_REVIEW.md` is two months and several fix-tickets stale — spot checks this pass
confirmed its P0 items 2, 3, 4 and 6 are already fixed (`whatsapp_bot.py:70-87` resolves
under `BROWSE_ROOT`; `web_chat.py:35` binds loopback; `web_chat.py:39` gates XFF behind
`WEB_CHAT_TRUST_PROXY`; `claude_core.py:427` maps permission modes rather than hardcoding
`bypassPermissions`; `store.py:543` adds the session lock from #8). Remaining findings
there should be re-derived rather than inherited.

New tickets from this document would number from **0028**.

---

## Second pass — verified source-level findings

*Added 2026-07-29 by a second review pass over the same three trees, against `main` @
`765fcba`. Items 1–27 above are unchanged.*

The document above defers source-level correctness to `docs/CODE_REVIEW.md`. **That
deferral no longer holds.** As its own closing note observes, that review is two months
stale and its remaining findings "should be re-derived rather than inherited" — but nobody
has done so, which leaves every source-level bug in this project either excluded from this
document or asserted by a document known to be unreliable. This section closes that gap: it
re-derives the review's open findings against current code, drops the ones already fixed
([Appendix A](#appendix-a--code_reviewmd-findings-verified-fixed)), and adds empirical
evidence from actually running the suite.

**Evidence base.** `pytest -q` → **2939 passed, 0 failed, 42.54s**.
`pytest --cov=telechat_pkg` → **84% total**:

| Module | Coverage | Uncovered statements |
|---|---:|---:|
| `desktop_bridge.py` | **19%** | 916 of 1130 |
| `telegram_bot.py` | 79% | 494 |
| `main.py` | 90% | 61 |
| `web_chat.py` | 93% | 17 |
| `whatsapp_bot.py` | 96% | 23 |
| `store.py` | 98% | 12 |
| `slack_bot.py` | 100% | 0 |
| **TOTAL** | **84%** | **1610 of 10315** |

Run with `telechat-features/venv` (Python 3.12.9) — this tree's default `python3` is 3.14.5
with no pytest, which is item 12's point exactly.

### 28. `AGENTS.md` tells every agent coverage is ~99%; it is 84%

**S (~15 min).** `AGENTS.md:24` describes `tests/` as "3000+ tests, ~99% coverage" and rule
8 repeats "~99% coverage today". Measured: **2939 tests, 84%**. Item 17 above notes the
claim "breaks down" for the bridge; the table above is what it actually is.

This matters more than a stale number normally would, because `AGENTS.md` is the file every
agent is instructed to read first, and a believed-99% figure actively suppresses the
test-writing this tree most needs — an agent that thinks coverage is near-total has no
reason to check. Replace the headline figure with either per-module numbers or a CI-enforced
floor (item 2), so it cannot drift again. Pairs with item 2 in wave 1.

> **Done (2026-07-30).** Both quoted figures are gone; `AGENTS.md` points at the enforced CI
> floor instead of a number that can go stale, and says outright that coverage is not uniform
> across modules. A test fails if a coverage percentage reappears in `AGENTS.md`.

### 29. `_db_writer` silently drops writes, never shuts down, and leaks its connection

**M (1–2 days for items 29–33 as one pass).** `store.py:93-112`, three defects in twenty
lines:

- `except Exception as e: log.error(...)` discards the **entire `ops` batch** on any
  failure. No retry, no dead-letter, no metric — a transient lock error silently loses
  conversation turns.
- `while True:` with no shutdown sentinel. `flush_writes()` (`store.py:139`, added since the
  last review — good) drains the queue but cannot stop the thread; as a daemon it is killed
  abruptly at interpreter exit.
- The thread's thread-local SQLite connection is never closed. **This is observably
  leaking:** the test run emits `ResourceWarning` tracebacks pointing at
  `store.py:98, in _db_writer` dozens of times.

Fix: sentinel-based shutdown, `conn.close()` in a `finally`, and bounded per-op retry before
dropping (log at `error` with the offending SQL when it finally gives up). The retry
counter is also the first useful entry in item 21's `/metrics`.

### 30. The queue-full fallback inverts write ordering

**Folded into item 29's pass.** `store.py:126-137` — `_enqueue_write` falls back to a
**synchronous** write on the caller's own connection when the queue is full
(`maxsize=1000`). That write commits immediately while earlier queued writes are still
pending, so ordering inverts: a sync `DELETE` can land before the queued `INSERT` it was
meant to follow. Under back-pressure — precisely when this path triggers — the database
reaches a state no sequential execution could produce.

Make the fallback block with a timeout instead of jumping the queue, or route everything
through the queue and let `put()` apply back-pressure. If a fast path must remain, gate it
on the queue being empty.

### 31. `save_turn` is three unrelated writes, and sub-millisecond turns vanish

**Folded into item 29's pass.** `store.py:344-360` — one logical turn enqueues three
independent ops (user INSERT, assistant INSERT, trim DELETE) with no transaction, so a crash
or queue-full between them leaves a half-saved conversation. Two further issues:

- Both INSERTs are `INSERT OR IGNORE` keyed on `(platform,user_id,ts)`, with the assistant
  row at `now + 0.001`. Two turns inside a millisecond collide and the second is **silently
  dropped** — `OR IGNORE` swallows it.
- The trim `DELETE ... OFFSET 20` runs against a table that may not yet contain the rows
  just enqueued, so the retention window drifts.

Fix: one queued op carrying a transaction, a monotonic sequence column rather than float
`ts` as row identity, and drop `OR IGNORE` so genuine conflicts raise.

### 32. `check_rate_limit` mutates shared state with no lock — the one place the lock was missed

**S (~1h).** `store.py:180-195`. `SessionManager` was correctly given an `RLock` since the
last review (`store.py:543`, held at 20+ call sites — the closing note above cites this as
resolving `CODE_REVIEW.md` #8). **`_rate_state` was missed.** It is a module-level dict
mutated via `setdefault` + list rebuild + append from the Telegram asyncio loop, the
WhatsApp worker thread, the Slack thread, and web-chat tasks concurrently. Lost updates let
a user exceed their limit, and the periodic stale-key sweep can `del` a key another thread
is mid-append on. Reuse the lock pattern already established a few hundred lines below.

### 33. The history cache has no cross-connection invalidation

**Folded into item 29's pass.** `store.py:339`. `_history_cache` is populated on the calling
thread's connection while the writer thread commits on its own, and nothing invalidates the
cache when the writer lands an INSERT or DELETE. Readers therefore serve stale history —
**Claude receives a conversation missing its most recent turns**, which presents as the bot
"forgetting" what was just said. Eviction is also a blunt `_history_cache.clear()` of the
entire cache, which under load simply clears repeatedly. Fix: a generation counter bumped by
the writer, or an LRU keyed per conversation with targeted invalidation on write.

### 34. Two residual hardening gaps that `CODE_REVIEW.md` records as resolved

**S each.** Both re-verified as live today:

- **MCP allowlist matches on basename only.** `mcp_client.py:64` — `os.path.basename(command)`
  is checked against `MCP_ALLOWED_COMMANDS`, so a poisoned `PATH` or a planted binary named
  `npx`/`python3` passes the check and executes. The env-scrubbing half of this finding *is*
  fixed (`mcp_client.py:89` passes only `_MCP_SAFE_ENV_PASSTHROUGH`), which is what the
  review's annotation was tracking. Resolve the command to an absolute path and allowlist
  that, or verify the resolved path sits in an expected prefix.
- **`pgrep -f telechat_pkg.main` still kills by substring.** `main.py:665` SIGTERMs every
  PID whose argv merely *contains* that string — an editor, a `grep`, a test harness — with
  no UID filter. A PID file in `~/.telechat/` is the correct fix. (The adjacent
  `lsof -ti :$HEALTH_PORT` kill at `main.py:681` was correctly narrowed to the configured
  port already.)

### 35. Retire `docs/CODE_REVIEW.md` rather than leaving it half-annotated

**S (~2h).** Of its seven P0 items, **five are fully fixed**; several `[PARTIALLY RESOLVED]`
annotations are now fully resolved (item 34's env scrubbing); and its line references are
stale throughout (`main.py` is 852 lines, not the 1083 it cites). Yet the document's
headline structure still reads as an open P0 list, and `AGENTS.md` points agents to it as
the canonical "code review notes".

Every future agent therefore pays a re-verification pass to discover that most of it is
history — which is the cost this section just absorbed. Convert it into a dated, explicitly
closed historical record, or delete it and let this document supersede it, with
[Appendix A](#appendix-a--code_reviewmd-findings-verified-fixed) as the disposition record.
Then repoint `AGENTS.md`. Do this in wave 1 alongside item 27 — both are "stop the tree
lying to its own agents" work.

> **Done (2026-07-30).** `docs/CODE_REVIEW.md` now opens with a closed-record banner naming
> this document as its successor and Appendix A as the disposition record, and says which
> part is still worth reading (§1's component map, for item 19). `AGENTS.md` points agents
> here instead, with the old review labelled closed.

### Revised sequencing for these items

Items 28 and 35 join **wave 1** (both are XS–S documentation-truth fixes, same character as
item 27). Items 29–33 form **one focused `store.py` pass in wave 2** — they overlap in the
same 250 lines, so batching avoids re-touching the code three times; schedule it alongside
item 5's guardrails and ahead of item 21's observability work, which wants the write-queue
counters this pass produces. Item 34's two hardening fixes can go to any agent in wave 4.

These are prioritised *above* items 18–26 deliberately: silent data loss in the persistence
layer outranks feature reach, and unlike most of this document's items it is invisible to
the user until history is already gone.

---

## Appendix A — `CODE_REVIEW.md` findings verified fixed

Re-derived against `main` @ `765fcba` so no one re-litigates them.

| Finding | Status today |
|---|---|
| Web chat exposed on `0.0.0.0` with no token | Fixed — defaults `127.0.0.1`, refuses to start exposed without a token (`web_chat.py:36,458-463`) |
| `X-Forwarded-For` trusted blindly | Fixed — gated on `WEB_CHAT_TRUST_PROXY` (`web_chat.py:38,70`) |
| WhatsApp `!cd`/`!view` sandbox escape | Fixed — `_safe_join` + `is_relative_to` (`whatsapp_bot.py:70-87`) |
| Hardcoded `permission_mode="bypassPermissions"` in the SDK path | Fixed — `sdk_perm_mode` (`claude_core.py:439`) |
| `os._exit(0)` in the SIGINT handler loses queued writes | Fixed for SIGINT — `flush_writes()` exists (`store.py:139`); the startup-path `os._exit(1)` remains, see item 11 |
| `session_manager.py` queried a non-existent `history` table | Fixed — `_ensure_schema` + legacy migration, 13 tests |
| MCP subprocess inherited full `os.environ` | Fixed — scrubbed to `_MCP_SAFE_ENV_PASSTHROUGH` (`mcp_client.py:89`); basename allowlist gap remains, see item 34 |
| `SessionManager._cache`/`_active` mutated without a lock | Fixed — `RLock` at `store.py:543`; **`_rate_state` was missed, see item 32** |
| Nine `test_100_*` / `test_coverage_*` padding files (~9800 LOC) | Fixed — collapsed into behavior-organized tests |
| Hand-rolled QR + Reed–Solomon in `main.py` | Fixed — removed; `main.py` now 852 lines |
| Optional dependencies undeclared | Fixed — extras `qr`/`sdk`/`docs`/`browser`/`httpx`/`mcp`/`dev`/`all` |
| Non-matching `scripts/*` package-data glob | Fixed |
| Stray `bot.db*`, `bot.err`, `coder_projects.json` in the tree | Fixed |
| No `.dockerignore` | Fixed (ticket 0021) — though the Dockerfile itself is broken, see item 1 |

Still live and carried into the items above: `save_turn` atomicity, `_db_writer` error
handling and connection leak, `_rate_state` lock, history-cache invalidation (items 29–33);
MCP basename allowlist and `pgrep -f` matching (item 34); `telegram_bot.py` size (items 6,
18); dependency upper bounds (item 7); ruff/mypy and pytest config (item 5); `except
Exception` prevalence (item 6); startup `os._exit` (item 11).

---

*Items 1–27 cover product, packaging, and process. Items 28–35 cover source-level
correctness, verified by execution. Together these supersede
[`docs/CODE_REVIEW.md`](CODE_REVIEW.md) — see item 35.*
