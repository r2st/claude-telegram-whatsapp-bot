---
id: 0016
title: Write per-module tests for 4 moderate-drop modules from 0012
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
---

## Goal

Four modules dropped 4–10 percentage points of coverage after ticket 0012 deleted `test_security_full.py`. They're not currently in any other ticket's scope and don't have dedicated `test_<module>.py` files. P2 because the modules still have feature-area test coverage; this is finishing-line work after the P1 heavy-drop tickets (0013/0014/0015) ship.

## Why it matters

Same rationale as ticket 0011: without dedicated per-module coverage, refactors and bug fixes lack a focused safety net. These modules don't sit on the user-facing critical path the way 0013–0015 do, but they're real surfaces that warrant focused tests.

## Acceptance criteria

For each module below, create `tests/test_<module>.py` organized by behavior, restore coverage to ≥95%, and confirm `pytest -q` still green:

- [ ] `browser_automation.py` (90% → 95%+) — playwright lifecycle, screenshot/text extraction, error paths
- [ ] `memory.py` (92% → 95%+) — FTS index build, recall, capacity / eviction
- [ ] `store.py` (92% → 95%+) — though significant, has substantial feature-area coverage via test_main.py et al; focus on uncovered store internals (write queue, history cache, session lifecycle)
- [ ] `cost_budget.py` (96% → 99%+) — small lift; likely just the no-rows / boundary case

## Likely files / surfaces touched

- 3–4 new `tests/test_<module>.py` files
- No source changes expected

## Notes

`memory.py` overlaps with CODE_REVIEW.md MEDIUM finding about MemoryStore opening its own thread-local connection pool against the same DB as store.py. Tests should establish current behavior; the consolidation (P1 #11) is a separate ticket.

`store.py` has the most existing coverage from feature tests — careful not to duplicate. Focus on the writer thread, the `_history_cache` LRU semantics, and the write-queue fallback path.

Created from ticket 0012.
