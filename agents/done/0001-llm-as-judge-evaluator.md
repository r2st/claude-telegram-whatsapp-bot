---
id: 0001
title: LLM-as-judge evaluator
role: builder
priority: P1
owner: opus-features
started: 2026-06-03
status: done
depends_on: []
touches:
  - telechat_pkg/evaluator.py
  - telechat_pkg/feedback.py
  - telechat_pkg/claude_core.py
  - telechat_pkg/telegram_bot.py
  - tests/test_evaluator.py
---

## Goal

Sample ~10% of bot responses and score them with a separate LLM
("judge") on a rubric (helpfulness, accuracy, tone). Feed scores into
the existing quality metrics so `/quality` reflects judged samples too.

## Why it matters

Today binary evaluators (`feedback.py`) catch trivial failures but
can't tell a mediocre answer from a good one. LLM-as-judge closes that
gap and feeds the self-improving loop.

## Acceptance criteria

- [ ] New `telechat_pkg/evaluator.py` module exposing
      `judge_response(prompt, response) -> JudgeScore`.
- [ ] Sampling rate configurable via env (`JUDGE_SAMPLE_RATE`, default 0.1).
- [ ] Scores persisted to `quality_scores` table (extend schema if needed,
      with migration).
- [ ] `/quality` command surfaces judge averages alongside binary metrics.
- [ ] Tests cover: sampling decision, judge call (mocked), score persistence.
- [ ] `pytest -q` green.
- [ ] `docs/implementation-tracker.md` row flipped to Done.

## Likely files / surfaces touched

- `telechat_pkg/evaluator.py` (new)
- `telechat_pkg/feedback.py` (call site)
- `telechat_pkg/claude_core.py` (schema + migration)
- `telechat_pkg/telegram_bot.py` (`/quality` output)
- `tests/test_evaluator.py` (new)

## Notes

Use Haiku for the judge to keep cost down. Judge prompt should ask for
a 1–5 score per dimension + a one-line justification — log the
justification so we can audit later.

## Outcome — 2026-06-03

Shipped `telechat_pkg/evaluator.py`: `judge_response()` (Haiku via injectable
`claude_fn`, degrades to no-op without an API key), `JUDGE_SAMPLE_RATE` (env,
default 0.1) sampling, robust JSON parsing with score clamping. Scores persist to
the existing `quality_scores` table (no migration needed — dimensions stored as
`judge_helpfulness/accuracy/tone/composite`, justification in metadata). Wired
into the Telegram response path via `_evaluate_quality`; new `/quality` command
surfaces binary + judge averages. `tests/test_evaluator.py` 29 tests, evaluator.py
100% coverage; full suite green modulo the 3 known cassette errors (0006).
