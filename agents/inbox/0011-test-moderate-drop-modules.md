---
id: 0011
title: Write per-module tests for 8 moderate-coverage-drop modules
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
---

## Goal

Eight modules dropped 6–12 percentage points of coverage after ticket 0007 deleted the padding test files. None had a dedicated `test_<module>.py`. This umbrella ticket tracks writing real behavior-organized tests for them. Each module is a separate unit of work but they're small enough to bundle into one tracking ticket rather than 8 individual ones.

## Why it matters

These modules currently rely on incidental coverage from feature-area tests (`test_telegram_e2e.py`, `test_new_features.py`, etc.) plus the gone-now padding tests. Without dedicated per-module coverage, refactors and bug fixes in them have no safety net. P2 because these are auxiliary modules — the P1 heavy-drop ones (0008, 0009, 0010) ship first.

## Acceptance criteria

For each module below, create `tests/test_<module>.py` organized by behavior, restore coverage to ≥95%, and confirm `pytest -q` still green:

- [ ] `auto_scheduler.py` (92% → target 95%+) — scheduler tick / job-fire timing / persistence
- [ ] `slack_bot.py` (92% → 95%+) — message handlers, slash commands, socket-mode lifecycle
- [ ] `two_agent.py` (90% → 95%+) — the two-agent collaboration loop / handoff
- [ ] `event_bus.py` (91% → 95%+) — pub/sub semantics, subscriber error isolation
- [ ] `knowledge_base.py` (93% → 95%+) — FTS index build, retrieval, document chunking
- [ ] `smart_router.py` (90% → 95%+) — model routing decisions for cost/latency
- [ ] `video_gen.py` (88% → 95%+) — generation request, polling, errors
- [ ] `music_gen.py` / `scheduled_tasks.py` / `voice_transcription.py` / `health.py` / `conversation_export.py` — finish-line items, only -1 to -7 each; bundle if the per-module file ends up tiny

## Likely files / surfaces touched

- 8+ new files in `tests/test_<module>.py`
- No source changes expected

## Notes

Can be split into individual tickets if any single module turns out to be a project of its own.

`slack_bot.py` already has feature coverage via `test_slack_e2e.py` (1281 LOC) — start there and identify what's not exercised before writing parallel unit tests. Same pattern for any other module whose feature-test file exists.

Created from ticket 0007.
