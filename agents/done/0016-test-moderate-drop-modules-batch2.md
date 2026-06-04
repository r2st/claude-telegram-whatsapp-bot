---
id: 0016
title: Write per-module tests for 4 moderate-drop modules from 0012
role: builder
priority: P2
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Four modules dropped 4–10 percentage points of coverage after ticket 0012 deleted `test_security_full.py`. They're not currently in any other ticket's scope and don't have dedicated `test_<module>.py` files. P2 because the modules still have feature-area test coverage; this is finishing-line work after the P1 heavy-drop tickets (0013/0014/0015) ship.

## Why it matters

Same rationale as ticket 0011: without dedicated per-module coverage, refactors and bug fixes lack a focused safety net. These modules don't sit on the user-facing critical path the way 0013–0015 do, but they're real surfaces that warrant focused tests.

## Acceptance criteria

For each module below, create `tests/test_<module>.py` organized by behavior, restore coverage to ≥95%, and confirm `pytest -q` still green:

- [x] `browser_automation.py` (90% → 95%+) — playwright lifecycle, screenshot/text extraction, error paths
- [x] `memory.py` (92% → 95%+) — FTS index build, recall, capacity / eviction
- [x] `store.py` (92% → 95%+) — though significant, has substantial feature-area coverage via test_main.py et al; focus on uncovered store internals (write queue, history cache, session lifecycle)
- [x] `cost_budget.py` (96% → 99%+) — small lift; likely just the no-rows / boundary case

## Likely files / surfaces touched

- 3–4 new `tests/test_<module>.py` files
- No source changes expected

## Notes

`memory.py` overlaps with CODE_REVIEW.md MEDIUM finding about MemoryStore opening its own thread-local connection pool against the same DB as store.py. Tests should establish current behavior; the consolidation (P1 #11) is a separate ticket.

`store.py` has the most existing coverage from feature tests — careful not to duplicate. Focus on the writer thread, the `_history_cache` LRU semantics, and the write-queue fallback path.

Created from ticket 0012.

## Outcome — 2026-06-03

All four modules tested per-module (each file run in isolation with
`COVERAGE_FILE=/tmp/.cov_0016`). Every file passes 100% of its tests.

| Module | Test file | Tests added | Before → After |
| --- | --- | --- | --- |
| `browser_automation.py` | `tests/test_browser_automation.py` (new) | 37 | 90% → **100%** |
| `memory.py` | `tests/test_memory.py` (extended; +28 in 6 new classes) | 90 total | 92% → **100%** |
| `store.py` | `tests/test_store.py` (new) | 91 | 92% → **98%** |
| `cost_budget.py` | `tests/test_cost_budget.py` (new) | 26 | 96% → **100%** |

Combined run of all four files: 241 passed, module-level coverage 99% overall.

### memory.py — new test classes (extends existing, none rewritten/deleted)
`TestGetById`, `TestFTSIndexBuild` (incl. trigger sync + FTS-MATCH
OperationalError → LIKE fallback), `TestSchemaUpgrade` (metadata-column ALTER
on legacy schema), `TestExportImport` (roundtrip, skip-empty, importance clamp,
created_at preservation), `TestExtractMemories` (async: empty / no-key fallback
/ truncation / successful API parse / API-error fallback / cached httpx
client), `TestDefaultDBPath` (db_path=None → store.DB_PATH). Tests pin the
*current* behavior of MemoryStore's separate thread-local connection pool;
consolidation remains a separate ticket.

### store.py — focus areas (no duplication of feature tests)
Writer thread (start/idempotent/restart-dead/batching/survives-bad-SQL),
write-queue sync fallback (no queue + full queue), `flush_writes` drain &
timeout, `_history_cache` TTL + LRU eviction (stale-sweep and full-clear
branches) + per-session-name key scoping, rate-limit bucket bookkeeping + stale
cleanup, conversation persistence (`replace_history`, `clear_history`,
`track_usage`/`get_usage`, `track_tool_usage`, `track_cost`), legacy
single-platform → multi-platform `init_db` migration, `UserSession` value
object, and full `SessionManager` lifecycle.

### Below target / untestable lines
- `store.py` left at **98%** (target ≥95%, met). 12 lines uncovered, all
  environmental / race-bound and intentionally left:
  - `31-36` — `_default_db_path()` `TELECHAT_HOME` branch + `OSError` guard;
    runs once at import time before conftest pins `DB_PATH`.
  - `82-83` — `_reset_conn_state()` exception-during-close guard.
  - `105-106` — writer inner-loop `get_nowait()` `Empty` break (timing race).
  - `312-313` — `init_db()` `desktop_bridge` import-failure swallow.

### Source bugs found (documented, NOT fixed per instructions)
- **`store.SessionManager.archive()` / `delete_by_name()` — archiving/deleting
  the sole session creates a duplicate in-memory `default`.** When the only
  remaining session is archived (or deleted) while it is the active one, the
  fallback branch appends a brand-new `UserSession("default", …)` but leaves the
  original archived object in the in-memory `_cache` list. Both share the
  `(platform, user_id, name)` UNIQUE key, so the list now holds two `"default"`
  entries and `get_or_create_active()` can return the *archived* one
  (`archived is True`). The DB INSERT also collides on the unique key
  (ON CONFLICT … DO UPDATE flips `archived` back to 0 for the row, but the stale
  in-memory copy is not reconciled). Test `test_archive_last_session_creates_default`
  pins the current (buggy) behavior rather than the intended one.
- **`cost_budget._get_daily_cost` / `_get_monthly_cost` dead fallback.** The
  `return 0.0, 0` after `if row:` (lines 127, 142) is unreachable in normal
  SQLite operation because the aggregate query (`COALESCE(SUM…)`, `COUNT(*)`)
  always returns exactly one row. Covered defensively via a mocked connection;
  noting it as effectively dead code, not a correctness bug.
