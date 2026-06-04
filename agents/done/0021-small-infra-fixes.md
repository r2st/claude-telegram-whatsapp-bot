---
id: 0021
title: Small infra fixes — `Optional[callable]` type hints + `.dockerignore`
role: builder
priority: P2
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
touches:
  - telechat_pkg/claude_core.py
  - .dockerignore
---

## Goal

Two small, low-risk cleanup items batched into one ticket:

1. **`Optional[callable]` → precise `Callable[...]` in `claude_core.py`** (CODE_REVIEW.md P2 #20). `callable` is the *builtin function* (the one you call to test "is this thing callable"), not a type. Type checkers reject it; current code works only because Python doesn't enforce hints at runtime. Replace with the actual callable signatures derived from how the callbacks are used in-function.

2. **Add `.dockerignore`** (CODE_REVIEW.md P2 #26). None exists. The current `Dockerfile` is itself broken (copies files that don't live at the repo root) but that's a separate fix; the `.dockerignore` is preventive — when the Dockerfile is fixed and starts using `COPY . .` or similar, the image must not ship `.env`, `*.db*`, `bot.err`, `.git/`, test fixtures, ADRs, etc.

## Why it matters

1. Type correctness — `Optional[callable]` is silently wrong; the moment mypy or ruff's type checking is wired in (CODE_REVIEW.md P2 #21, still open), every call site becomes a noisy error. Fixing now is a few-line PR; later it's a sea of red.

2. Image hygiene — without `.dockerignore`, a future `COPY . .` would ship the operator's `.env` (the WIP commit's `.env.example` confirms this file holds real secrets per CODE_REVIEW.md §9 MEDIUM), `bot.db*` (runtime data), `bot.err` (4.9 MB stale log before 0005 cleanup, regenerable), and all of `agents/`, `docs/`, `.git/`. Big attack surface for zero effort to add a 30-line file.

## Acceptance criteria

- [x] `from typing import Awaitable, Callable, Optional` (line 14 of `claude_core.py`)
- [x] 8 occurrences of `Optional[callable]` replaced across 3 function signatures (lines 125-127, 358-359, 404-406). Final state verified with `grep -n "Optional\[callable\]" telechat_pkg/claude_core.py` returning empty.
- [x] `.dockerignore` created — 60 LOC covering secrets (.env, *.pem, credentials.json), runtime data (*.db*, bot.err, bot.log, *.pid, watchdog state), VCS (.git, .gitignore, .github), Python build/test artifacts, type/lint caches, virtual envs, tests dir, agents/ coordination scaffolding, docs/, IDE/OS metadata, node_modules, local dev overrides
- [x] `pytest tests/test_claude_core.py -q` green: 100 passed in 5.50s. Type-hint change was runtime no-op as expected.
- [x] `agents/check-overlap.sh 0021` reported "no overlap. Safe to claim 0021."

## Likely files / surfaces touched

See `touches:` above.

## Notes

- Other agent has been active in this branch but **has not touched `claude_core.py`** (last commit to it: `1104384`, the WIP snapshot — that's mine). Low collision risk.
- Closes CODE_REVIEW.md P2 #20 and #26 completely.
- The "fix the Dockerfile to actually work" item is **not** in scope — Dockerfile copies `main.py claude_core.py ...` at the root, but code lives in `telechat_pkg/`. Broken since at least the May review. File a separate ticket if the deployment story matters.
- `_bool_env` helper unification (CODE_REVIEW §6 MEDIUM) is also in the "smalls" bucket but deliberately not bundled here — it touches many files across the package, high collision risk with the active other agent who is writing per-module tests.

## Outcome — 2026-06-03

Two small fixes shipped:

1. `claude_core.py` — replaced 8 occurrences of `Optional[callable]` (which type-checks as nonsense because `callable` is a builtin function, not a type) with precise signatures: `Optional[Callable[[], bool]]` for the sync `is_cancelled` predicate, `Optional[Callable[[str, str], Awaitable[None]]]` for `on_progress`, `Optional[Callable[[str], Awaitable[None]]]` for `on_text`. Added `Callable, Awaitable` to the typing import. Closes CODE_REVIEW.md P2 #20.

2. `.dockerignore` — created from scratch, 60 LOC. Defense-in-depth against a future Dockerfile that uses `COPY . .`: blocks .env, *.db*, bot.err/log, .git, agents/, docs/, tests/, build artifacts, caches, virtual envs, IDE/OS metadata. Closes CODE_REVIEW.md P2 #26.

Runtime impact: zero. `pytest tests/test_claude_core.py -q` → 100 passed in 5.5s.

Discovered while writing this ticket but **not in scope**: the existing `Dockerfile` is broken — it COPYs `main.py claude_core.py telegram_bot.py whatsapp_bot.py slack_bot.py bot.py` at the repo root, but the code lives in `telechat_pkg/`. So even with the new `.dockerignore` in place, the image as built today contains none of the actual application code. Separate ticket needed for the deployment story.
