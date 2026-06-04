---
id: 0013
title: Write behavior-organized tests for telechat_pkg/resource_limiter.py
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
---

## Goal

Create `tests/test_resource_limiter.py` covering the resource_limiter module's real behaviors. Coverage dropped 100% → **51%** after ticket 0012 deleted `test_security_full.py` — 72 lines now unexercised, the single biggest drop from that ticket.

## Why it matters

resource_limiter enforces per-user / per-platform constraints on Claude calls — CPU time, concurrent requests, memory, subprocess spawn rate. When it gets thresholds wrong (or skips them silently), the bot either rate-limits aggressively for honest users or leaks resources to abuse. The padding tests were structured as `class TestResourceLimiterExecute`, `class LastMileResourceLimiter` — line-pegged at the highest resolution. They covered the lines but didn't assert the actual rate-limiting decisions. Replace with behavior-organized tests that assert: "when over budget → request denied with reason X", not "the budget-check code path executed".

## Acceptance criteria

- [ ] `tests/test_resource_limiter.py` created, organized by behavior
- [ ] Coverage of `telechat_pkg/resource_limiter.py` returns to ≥95%
- [ ] No class named `LastMile*`, `*Execute`, or any branch-pegged form
- [ ] `pytest -q tests/test_resource_limiter.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_resource_limiter.py` (new)
- No source changes expected

## Notes

Key behaviors to cover, derived from the module surface and CODE_REVIEW.md context:

1. Concurrent-request cap: Nth request beyond cap → rejected
2. Memory limit: process over threshold → throttled with the right kind of error
3. CPU time / subprocess timeout enforcement
4. Per-user vs global limits — fairness invariant
5. Limit reset / window expiry

Created from ticket 0012.
