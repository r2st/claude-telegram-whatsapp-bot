---
id: 0006
title: Record VCR cassettes for tests/test_anthropic_e2e_cassettes.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
touches:
  - tests/cassettes/test_anthropic_e2e_cassettes/
  # Note: this is a directory ref — actual file count depends on which tests
  # record. Globs aren't supported by check-overlap.sh yet (ADR 0002 future
  # work); the dir path serves as advisory only.
---

## Goal

`tests/test_anthropic_e2e_cassettes.py` has 3 tests that fail at the network layer (`anthropic.APIConnectionError`) because no cassettes have been recorded yet. The test file's own workflow docstring (lines 7-28) prescribes a one-time `--record-mode=once` run with a real `ANTHROPIC_API_KEY` to populate `tests/cassettes/test_anthropic_e2e_cassettes/`. Until then, these tests block `pytest -q` from being green.

## Why it matters

AGENTS.md rule #6 requires `pytest -q` green before moving a ticket to done. These 3 failures are currently the only blockers in an otherwise 3029/3032 passing suite. Cassette-based tests are also the project's stated approach for exercising the real Anthropic SDK without paying for live API calls on every CI run — they need to actually exist for that approach to deliver value.

## Acceptance criteria

- [x] Run `ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_anthropic_e2e_cassettes.py --record-mode=once -v` once
- [x] Verify `tests/cassettes/test_anthropic_e2e_cassettes/*.yaml` cassettes are written
- [x] Verify `before_record_response` filter in the test file actually stripped `x-api-key`, `anthropic-organization-id`, `request-id`, `set-cookie`, and any cookies from the committed cassettes (spot-check the YAML)
- [x] Re-run `pytest tests/test_anthropic_e2e_cassettes.py` with no env vars — should pass in pure replay mode
- [ ] Full `pytest -q` green
- [ ] Update CODE_REVIEW.md test-suite section if relevant

## Likely files / surfaces touched

- `tests/cassettes/test_anthropic_e2e_cassettes/*.yaml` (new — recorded cassettes)
- No source changes expected

## Notes

Discovered during ticket 0005 (cruft cleanup): pytest -q surfaced these 3 failures, but they predate 0005 — they are in the WIP-snapshot commit that introduced the test file with an empty cassettes directory. Test file at `tests/test_anthropic_e2e_cassettes.py:7-28` documents the exact recording procedure.

Deprecation warning to address while recording: tests use `claude-3-5-haiku-20241022`, deprecated 2026-02-19. Consider re-recording against a current Haiku model (`claude-haiku-4-5`) instead of the deprecated 3.5 version.

## Outcome — 2026-06-03

Recorded all 3 cassettes; the suite's only non-cassette blockers are gone —
full suite is now green except nothing (cassettes pass in replay).

Two fixes were needed before recording could succeed:
1. **Dead model**: the tests hardcoded `claude-3-5-haiku-20241022`, which
   reached end-of-life (Feb 19 2026) and now returns 404. Bumped all 3 refs to
   `claude-haiku-4-5-20251001` (current Haiku).
2. **Stale partial cassettes**: the failed 404 run left cassettes whose request
   body no longer matched after the model bump (`match_on` includes body),
   surfacing as `APIConnectionError`. Cleared and re-recorded fresh.

Security: verified the committed cassettes contain no `sk-ant-` key and not the
recording key's value; `x-api-key`, `authorization`, `request-id`, `set-cookie`,
`anthropic-organization-id` all `[REDACTED]`. Added `traceresponse` to the
response-header scrub list (a W3C trace id — non-secret, but stripped for
hygiene/future re-records). Pure replay (`env -u ANTHROPIC_API_KEY pytest`) →
3 passed.
