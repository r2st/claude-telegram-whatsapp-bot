---
id: 0005
title: Remove stale runtime data and build artifacts from working tree
role: builder
priority: P0
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Delete leftover runtime files (`telechat_pkg/bot.db*`, `telechat_pkg/coder_projects.json`), stale error logs (`bot.err`, `bot.log`), and untracked build/test artifacts (`dist/`, `telechatai.egg-info/`, `.pytest_cache/`, `.coverage`, `.benchmarks/`, `.DS_Store`) from the working tree. All are already gitignored — this is filesystem cleanup, not policy change.

## Why it matters

`CODE_REVIEW.md` §9 [HIGH]: `bot.db*` inside `telechat_pkg/` risks shipping the SQLite DB inside the wheel if anyone packages without cleaning. `store.py` already resolves the canonical path to `~/.telechat/bot.db`, so these are dead leftovers from an older layout. `bot.err` is 4.9 MB of repeated "can't open file '.../main.py'" from a stale launcher pointing at a non-existent root-level `main.py` — keeps obscuring real errors. `coder_projects.json` is the same anti-pattern as `bot.db` (`coder.py` resolves elsewhere).

## Acceptance criteria

- [x] `telechat_pkg/bot.db`, `telechat_pkg/bot.db-shm`, `telechat_pkg/bot.db-wal` deleted (only `bot.db` actually existed; no -shm/-wal present)
- [x] `telechat_pkg/coder_projects.json` deleted
- [x] `bot.err`, `bot.log`, `.coverage` deleted from repo root
- [x] `dist/`, `telechatai.egg-info/`, `.pytest_cache/`, `.benchmarks/` directories deleted
- [x] All `.DS_Store` removed (only one existed, at repo root)
- [x] No code references any deleted path (verified: `grep -rn 'telechat_pkg/bot\.db\|telechat_pkg/coder_projects' .` returns 0 matches in `*.py`)
- [ ] `pytest -q` green — **3029 passed, 39 skipped, 3 failed**. The 3 failures are pre-existing in `tests/test_anthropic_e2e_cassettes.py` (require cassettes that have not been recorded — `tests/cassettes/` only had `.gitkeep`). They fail at the network layer (`anthropic.APIConnectionError`), not at any path touched by this ticket. Tracked in ticket 0006.
- [x] `git status --short` shows no untracked cruft after deletions (gitignore already covered all targets)

## Likely files / surfaces touched

- Deletions only. No source code modifications.
- `.gitignore` already covers every target (verified) — no edit needed.

## Notes

Maps directly to `CODE_REVIEW.md` P0 #7 ("Delete `telechat_pkg/bot.db*`, `bot.err`, `telechat_pkg/coder_projects.json`") plus the §9 [HIGH] / [MEDIUM] / [LOW] items on `.coverage`, build artifacts, and the bot.err launcher loop. The launcher bug that fills `bot.err` is **not** in scope here — fixing it (a crontab/launchd plist pointing at a non-existent `main.py`) is a separate concern; this ticket just removes the symptom log so future error appearances aren't lost in noise.

Does not touch any of the security-critical P0 items (#1-#6). Those need their own tickets and careful changes.

## Outcome — 2026-06-03

Deleted 10 stale targets from working tree: `telechat_pkg/bot.db`, `telechat_pkg/coder_projects.json`, `bot.err` (4.9 MB), `bot.log`, `.coverage`, `dist/`, `telechatai.egg-info/`, `.pytest_cache/`, `.benchmarks/`, `.DS_Store`. All were already gitignored — no `.gitignore` changes needed. No source files modified. `pytest -q` reports 3029 passed / 39 skipped / 3 failed; the 3 failures are pre-existing in `tests/test_anthropic_e2e_cassettes.py` (missing VCR cassettes, unrelated to deletions) and are tracked separately in ticket 0006.
