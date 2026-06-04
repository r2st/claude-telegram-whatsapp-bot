---
id: 0008
title: Write behavior-organized tests for telechat_pkg/commitments.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Create `tests/test_commitments.py` covering the commitments module's real behaviors. Currently no per-module test file exists; coverage was 100% pre-0007 entirely via line-pegged padding tests, dropped to 70% after deletion (35 lines uncovered).

## Why it matters

Commitments is the user-facing "you said you'd do X" feature surface. With 30% of its statements unexercised by any test, regressions in the commit/recall/expiry/list paths can ship silently. The padding tests that "covered" it didn't catch behavioral regressions — they asserted line execution, not semantics. Replace with tests organized by what the module *does*.

## Acceptance criteria

- [ ] `tests/test_commitments.py` created, organized by behavior (not by source line)
- [ ] Test classes named after the user-facing concept (e.g. `TestRecordingCommitments`, `TestRecallingByDate`, `TestExpiry`) — not `TestLine<NNN>` or `TestModule100`
- [ ] Coverage of `telechat_pkg/commitments.py` returns to ≥95%
- [ ] `pytest -q tests/test_commitments.py` green
- [ ] Full `pytest -q` still green (modulo pre-existing 3 cassette failures from 0006)
- [ ] No `assert <thing>` for assertions whose only justification is "line hit" — every assertion has a behavioral reason a reader can understand

## Likely files / surfaces touched

- `tests/test_commitments.py` (new)
- No source changes expected

## Notes

Run `pytest --cov=telechat_pkg.commitments --cov-report=term-missing tests/test_commitments.py` while developing to see which lines still need coverage. Then ask: is the uncovered line a behavior worth testing? If yes, add a test that asserts that behavior. If no (e.g., a defensive `except` that can't be triggered by real input), leave it uncovered and add a `# pragma: no cover` comment.

Created from ticket 0007 (collapse coverage-padding tests). The padding files that previously gave commitments.py 100% coverage were `test_full_coverage.py`, `test_100_coverage.py`, etc. — they have been deleted. Their content is in `git log -p b35e07d..HEAD -- tests/` if you want to see what specifically was being touched, but the goal is to write *new* tests organized by behavior, not port the old ones.

## Outcome — 2026-06-03

- **File created:** `tests/test_commitments.py` (new, behavior-organized).
- **Tests:** 75 passing (some via `parametrize`), grouped into behavior classes:
  `TestParsingDueTime`, `TestDaysUntilWeekday`, `TestExtractCommitments`,
  `TestRecordingCommitments`, `TestRecallingByDateAndExpiry`,
  `TestStatusTransitions`, `TestAutoExtractAndStore`, `TestFormatPending`,
  `TestInitDb`, `TestParseRow`. No `TestLine<NNN>` classes; every assertion
  checks a semantic outcome.
- **Coverage:** `telechat_pkg/commitments.py` 70% → **100%** (116/116 statements),
  exceeding the ≥95% target. No lines left uncovered; no `# pragma: no cover`
  needed.
- **Verification command (passes 100%):**
  `COVERAGE_FILE=/tmp/.cov_0008 python -m pytest -q tests/test_commitments.py --cov=telechat_pkg.commitments --cov-report=term-missing`
- **Fixture approach:** a local `db` fixture points `store.DB_PATH` at a per-test
  temp SQLite file, resets the thread-local connection cache, and leaves
  `store._write_queue = None` so `_enqueue_write` writes synchronously (the
  background writer thread is never started) — making CRUD writes immediately
  visible to reads in the same thread. State is restored after each test.
- **Caveats:** time-window display assertions (`in 3d`/`in 2h`/`in 5m`) use a
  small forward buffer to avoid `timedelta.days`/`.seconds` truncation flake from
  the microseconds elapsed between the two `time.time()`/`datetime.now()` calls.
- **Source bugs found:** none. Behavior matched the module's documented intent
  throughout (extraction dedup, default-24h fallback, snooze/dismiss/sent
  semantics, due-time parsing precedence).
