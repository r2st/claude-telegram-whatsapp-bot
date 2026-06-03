---
id: 0008
title: Write behavior-organized tests for telechat_pkg/commitments.py
role: builder
priority: P1
owner:
started:
status: inbox
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
