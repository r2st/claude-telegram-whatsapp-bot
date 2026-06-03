---
id: 0010
title: Write behavior-organized tests for telechat_pkg/doctor.py
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
---

## Goal

Create `tests/test_doctor.py` covering the doctor (diagnostic / setup-check) module's real behaviors. Currently no per-module test file exists; coverage was 100% pre-0007 via padding tests, dropped to 75% after deletion (42 lines uncovered — the largest absolute LOC drop).

## Why it matters

`doctor.py` is what runs when a user invokes `telechat doctor` to figure out why the bot isn't working. If doctor itself has bugs, the user gets *worse* diagnostics than no doctor at all — false greens, missing warnings, crashes mid-check. Behavior-organized tests around each check's pass/fail/skip outcomes (and the report format) catch the failure modes that matter at install/upgrade time.

## Acceptance criteria

- [ ] `tests/test_doctor.py` created, organized by what each diagnostic check verifies
- [ ] Test classes named after the user-facing diagnostic (e.g. `TestPythonVersionCheck`, `TestTelegramTokenCheck`, `TestSQLiteWritableCheck`, `TestClaudeInPathCheck`)
- [ ] Each check has tests for the pass case, the fail case, AND the report-format / human-readability of the failure message (doctor's output is what the user reads)
- [ ] Coverage of `telechat_pkg/doctor.py` returns to ≥95%
- [ ] `pytest -q tests/test_doctor.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_doctor.py` (new)
- No source changes expected

## Notes

When testing checks that probe external state (sqlite, network, subprocess), prefer monkeypatching the boundary call rather than mocking the check internals — that way the tests can be written in terms of "doctor reports X when sqlite is read-only" instead of "doctor calls function Y with arg Z".

Created from ticket 0007.
