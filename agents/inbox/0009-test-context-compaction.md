---
id: 0009
title: Write behavior-organized tests for telechat_pkg/context_compaction.py
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
---

## Goal

Create `tests/test_context_compaction.py` covering the conversation-compaction module's real behaviors. Currently no per-module test file exists; coverage was 100% pre-0007 via padding tests, dropped to 69% after deletion (28 lines uncovered).

## Why it matters

Context compaction is the seam where token budgets meet conversation history — when it gets it wrong, users either run out of context mid-conversation (no compaction firing) or lose context they needed (compaction too aggressive). Both failure modes are subtle and don't trip the existing feature tests. Behavior-organized tests around budget thresholds, extractive-vs-claude_fn paths, and the `keep_recent` guarantee will catch regressions the line-pegged tests couldn't.

## Acceptance criteria

- [ ] `tests/test_context_compaction.py` created, organized by behavior
- [ ] Test classes named for what the user observes (e.g. `TestKeepRecentInvariant`, `TestExtractiveFallback`, `TestBudgetThreshold`, `TestClaudeFnPath`)
- [ ] Coverage of `telechat_pkg/context_compaction.py` returns to ≥95%
- [ ] `pytest -q tests/test_context_compaction.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_context_compaction.py` (new)
- No source changes expected

## Notes

Key behaviors to cover, derived from the module surface:

1. `keep_recent` always preserved — never compact a message inside that window
2. `claude_fn=None` falls back to `_extractive_summary` (the original padding-test focus was line 164 — now uncovered)
3. Token budget threshold: compaction fires when history exceeds `max_tokens`, doesn't fire below
4. Result shape: `result.history` is a list, `result.summary_tokens > 0` when compaction ran

The deleted `test_100_coverage.py::TestContextCompactionLine164` is an antipattern, not a template — its docstring says *"Line 164: summary = _extractive_summary(old_messages) when claude_fn is None"*. The test asserted the line ran; we want to assert the *invariant* (extractive summary is non-empty when no LLM is available).

Created from ticket 0007.
