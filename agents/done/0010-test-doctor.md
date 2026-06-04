---
id: 0010
title: Write behavior-organized tests for telechat_pkg/doctor.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
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

## Outcome — 2026-06-03

- Created `tests/test_doctor.py` — 55 tests, all green.
- Coverage of `telechat_pkg/doctor.py`: 75% → **100%** (170 stmts, 0 missed),
  verified with `COVERAGE_FILE=/tmp/.cov_0010 python -m pytest -q tests/test_doctor.py --cov=telechat_pkg.doctor --cov-report=term-missing`.
- Organized by user-facing diagnostic: `TestPythonVersionCheck`,
  `TestClaudeInPathCheck`, `TestEnvFileCheck`, `TestTelegramTokenCheck`,
  `TestSQLiteWritableCheck`, `TestDiskSpaceCheck`, `TestDependenciesCheck`,
  `TestRateLimitsCheck`, `TestAllowedUsersCheck`,
  `TestTelegramConnectivityCheck`, plus `TestDoctorReport`/`TestReportFormat`
  (report plumbing + human-readable output) and `TestRunDoctorSync`/`TestRunDoctor`
  (orchestration). Every check covers pass, fail, and the failure
  message/severity/fix_hint wording.
- Boundaries monkeypatched, not internals: `shutil.which`, `shutil.disk_usage`,
  `store._get_conn`, `builtins.__import__`, env vars, and a fake `aiohttp`
  module injected into `sys.modules` for the async connectivity check.

### Caveats / notes
- No source changes made; no source bugs found. `doctor.py` behaves correctly.
- `check_python_version` reads `v.major/.minor/.micro`, so the fail-path test
  monkeypatches `sys.version_info` with a 3-field namedtuple (tuple-comparable
  + attribute access) rather than a plain tuple.
- Minor wording quirk (not a bug, left as-is): access-control message says
  "Slack (1 users)" — no singular/plural handling. Asserted as-is.
- Constraints honored: only `tests/test_doctor.py` created; no edits to
  conftest, source, or other test files; ran only this module; coverage file
  pinned to `/tmp/.cov_0010`.
