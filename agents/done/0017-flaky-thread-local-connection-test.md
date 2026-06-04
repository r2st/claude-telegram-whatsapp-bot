---
id: 0017
title: Fix flaky test_store_thread_local_connections (intermittent full-suite failure)
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

`tests/test_security.py::TestConcurrentAccess::test_store_thread_local_connections`
fails intermittently in the full suite (observed 1 of 5 runs after ticket
0012). It passes 173/173 when `test_security.py` is run alone and passes
in isolation in <0.1s. The flake was masked by `test_security_full.py`
(deleted in 0012, whose 374 tests changed the pre-run state/timing) and
slipped past 0012's outcome, which recorded a flat "3 failed (cassettes)"
from a lucky run.

## Why it matters

This is a real test-isolation defect, not coverage furniture. The test
spawns 5 threads, each calling `telechat_pkg.store._get_conn()`, and
asserts `len(connections) == 5`. When a prior test leaves the store's
module-global thread-local / db-path state in a bad condition, one or more
threads raise inside `_get_conn()` (the exception dies in the worker
thread, so only the count assertion catches it) and the assert fails with
<5 connections. A suite that goes red ~20% of runs erodes trust in CI and
will be "fixed" by reflexive reruns instead of diagnosis — exactly the
anti-pattern tickets 0007/0012 set out to remove.

## Acceptance criteria

- [ ] Reproduce the failure deterministically (find a test-ordering or
      prior-state condition that makes it fail every time — e.g. run the
      full `tests/` dir under `--cov`, or bisect which preceding test
      pollutes `store` module globals).
- [ ] Root cause identified: which global in `telechat_pkg/store.py`
      (`_get_conn` thread-local, `_db_path`, write-queue thread, etc.) is
      left dirty, and by which test/fixture.
- [ ] Fix is isolation-correct: either the fixture (`store_with_db`)
      resets store globals on setup/teardown, or `_get_conn()` is made
      robust to a reset db path. No `skipif`/`xfail`/retry band-aids.
- [ ] `python -m pytest tests/ --cov=telechat_pkg` green modulo the 3
      known cassette failures (0006) across 5 consecutive runs.
- [ ] No new shared-state leakage introduced for the sibling threading
      tests (`test_memory_store_thread_local`, `test_coder_project_file_locking`).

## Likely files / surfaces touched

- `tests/test_security.py` (the `store_with_db` fixture / test)
- `tests/conftest.py` (if store-global reset belongs there)
- `telechat_pkg/store.py` (only if `_get_conn` itself needs hardening)
- No production behavior change expected — this is test hygiene.

## Notes

Discovered while independently validating ticket 0012's coverage delta
(numbers confirmed: 82% → 79%). The 0012 done-ticket and its commit
(46d522a) state "green modulo 3 cassette failures"; that holds for ~4 of 5
runs but not all — this ticket tracks the remaining flake. Observed once as
`4 failed, 2019 passed` (the 4th being this test) under a `--cov` full-suite
run; all other observed runs were `3 failed, 2020 passed`.

Not pytest-randomly (not installed). Suspect store module-global state
left dirty by a preceding test combined with thread-timing sensitivity.
Related: the deleted `test_security_full.py` had `test_db_writer_batches`
marked `skipif CI != "true"` with comment "flaky in full-suite due to
shared state" — same root family (store globals + threads), and 0012's
own "Why it matters" called that out. Worth checking whether the store
write-queue background thread is the shared culprit for both.

Created from independent verification of ticket 0012.


## Outcome — 2026-06-03

**Root cause:** not test-ordering pollution but a genuine concurrency race in
`store._get_conn()`. The test opens 5 thread-local SQLite connections at once
against a brand-new temp DB. The first connection's `PRAGMA journal_mode=WAL`
switch needs a brief exclusive lock; the other threads, opening concurrently
with no busy timeout, raised `sqlite3.OperationalError: database is locked`,
died in the worker thread, and never appended — so `len(connections) < 5`.
`test_security_full.py`'s 374 extra tests had been warming the WAL DB before
this test ran, masking the race; deleting it (0012) exposed it.

**Fix (isolation-correct, no band-aids):**
- `telechat_pkg/store.py` `_get_conn()`: open with `timeout=30.0`, set
  `PRAGMA busy_timeout=30000` *before* the WAL switch so concurrent openers
  wait for the lock instead of erroring; publish `_local.conn` only after full
  init so a mid-setup failure can't cache a half-configured handle.
- `telechat_pkg/store.py`: added `_reset_conn_state()` — closes and drops the
  thread-local connection cache (a connection caches the `DB_PATH` it opened
  against; tests that repoint `DB_PATH` at a temp DB must clear it).
- `tests/test_security.py` `store_with_db` fixture: call `_reset_conn_state()`
  on setup and teardown; convert `return` → `yield`.

**Verification:** full suite under `--cov` (now 2837 tests incl. the new
per-module files), **5 consecutive runs all `2834 passed, 3 failed`** — the 3
being only the known unrecorded-cassette failures (0006). The previously-flaky
`test_store_thread_local_connections` no longer appears in any failure list.
Production change is minimal and behavior-preserving (busy_timeout only affects
lock-wait behavior; reset helper is test-only).
