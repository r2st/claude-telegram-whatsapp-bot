---
id: 0005
title: Remove stale runtime data and build artifacts from working tree
role: builder
priority: P0
owner:
started:
status: inbox
depends_on: []
---

## Goal

Delete leftover runtime files (`telechat_pkg/bot.db*`, `telechat_pkg/coder_projects.json`), stale error logs (`bot.err`, `bot.log`), and untracked build/test artifacts (`dist/`, `telechatai.egg-info/`, `.pytest_cache/`, `.coverage`, `.benchmarks/`, `.DS_Store`) from the working tree. All are already gitignored — this is filesystem cleanup, not policy change.

## Why it matters

`CODE_REVIEW.md` §9 [HIGH]: `bot.db*` inside `telechat_pkg/` risks shipping the SQLite DB inside the wheel if anyone packages without cleaning. `store.py` already resolves the canonical path to `~/.telechat/bot.db`, so these are dead leftovers from an older layout. `bot.err` is 4.9 MB of repeated "can't open file '.../main.py'" from a stale launcher pointing at a non-existent root-level `main.py` — keeps obscuring real errors. `coder_projects.json` is the same anti-pattern as `bot.db` (`coder.py` resolves elsewhere).

## Acceptance criteria

- [ ] `telechat_pkg/bot.db`, `telechat_pkg/bot.db-shm`, `telechat_pkg/bot.db-wal` deleted
- [ ] `telechat_pkg/coder_projects.json` deleted
- [ ] `bot.err`, `bot.log`, `.coverage` deleted from repo root
- [ ] `dist/`, `telechatai.egg-info/`, `.pytest_cache/`, `.benchmarks/` directories deleted
- [ ] All `.DS_Store` removed
- [ ] No code references any deleted path (verified: `grep -rn 'telechat_pkg/bot\.db\|telechat_pkg/coder_projects' .` returns 0 matches in `*.py`)
- [ ] `pytest -q` green
- [ ] `git status --short` shows no untracked cruft after deletions (gitignore already covers)

## Likely files / surfaces touched

- Deletions only. No source code modifications.
- `.gitignore` already covers every target (verified) — no edit needed.

## Notes

Maps directly to `CODE_REVIEW.md` P0 #7 ("Delete `telechat_pkg/bot.db*`, `bot.err`, `telechat_pkg/coder_projects.json`") plus the §9 [HIGH] / [MEDIUM] / [LOW] items on `.coverage`, build artifacts, and the bot.err launcher loop. The launcher bug that fills `bot.err` is **not** in scope here — fixing it (a crontab/launchd plist pointing at a non-existent `main.py`) is a separate concern; this ticket just removes the symptom log so future error appearances aren't lost in noise.

Does not touch any of the security-critical P0 items (#1-#6). Those need their own tickets and careful changes.
