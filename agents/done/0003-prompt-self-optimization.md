---
id: 0003
title: Prompt self-optimization (A/B test system prompts)
role: builder
priority: P2
owner: opus-features
started: 2026-06-03
status: done
depends_on: [0001]
touches:
  - telechat_pkg/prompt_optimizer.py
  - telechat_pkg/claude_core.py
  - telechat_pkg/telegram_bot.py
  - tests/test_prompt_optimizer.py
# evaluator.py is read-only from this ticket (created by 0001) — not listed.
---

## Goal

Maintain N candidate system prompts, route a fraction of traffic to
each, and promote the winner based on judge scores + user ratings.

## Why it matters

The "self-improving" claim in the architecture is only real if the
prompts themselves improve. This is the mechanism.

## Acceptance criteria

- [ ] New `telechat_pkg/prompt_optimizer.py` managing prompt variants.
- [ ] Schema: `prompt_variants` (id, text, traffic_weight, score, status).
- [ ] Routing: assign each request to a variant by weight; record which.
- [ ] Scoring: aggregate judge scores + /rate per variant.
- [ ] Promotion: when one variant beats baseline by threshold over N
      samples, increase its weight; demote losers.
- [ ] Admin command (`/prompts`) to list variants and force-promote.
- [ ] Tests: assignment determinism per request, score aggregation,
      promotion logic.
- [ ] `pytest -q` green.
- [ ] `docs/implementation-tracker.md` row flipped to Done.

## Likely files / surfaces touched

- `telechat_pkg/prompt_optimizer.py` (new)
- `telechat_pkg/claude_core.py` (schema + prompt selection hook)
- `telechat_pkg/evaluator.py` (score source — depends on 0001)
- `telechat_pkg/telegram_bot.py` (`/prompts`)
- `tests/test_prompt_optimizer.py` (new)

## Notes

Depends on 0001 (LLM-as-judge) for the scoring signal. Start with
2 variants (current + one alternate) before going wider. Lock variant
IDs in conversation memory so a single conversation stays on one
variant for coherence.

## Outcome — 2026-06-03

Shipped `telechat_pkg/prompt_optimizer.py`: `prompt_variants` /
`prompt_assignments` / `prompt_variant_scores` tables (module-owned), weighted
`assign_variant` with sticky per-conversation routing, `record_score` +
`variant_stats` aggregation, `maybe_promote` (bump winners / retire losers past
`min_samples` and `margin`), and `force_promote`. Integrated into Telegram behind
`PROMPT_OPTIMIZER_ENABLED` (default off): `_select_system_prompt` routes to a
variant, `_evaluate_quality` attributes the binary composite back to it, and the
new `/prompts` admin command lists/add/promote. Depends on 0001 (judge scores feed
the same per-variant signal). `tests/test_prompt_optimizer.py` 18 tests, 100% cov.
