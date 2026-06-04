---
id: 0011
title: Write per-module tests for 8 moderate-coverage-drop modules
role: builder
priority: P2
owner: claude-opus-4-8
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Eight modules dropped 6–12 percentage points of coverage after ticket 0007 deleted the padding test files. None had a dedicated `test_<module>.py`. This umbrella ticket tracks writing real behavior-organized tests for them. Each module is a separate unit of work but they're small enough to bundle into one tracking ticket rather than 8 individual ones.

## Why it matters

These modules currently rely on incidental coverage from feature-area tests (`test_telegram_e2e.py`, `test_new_features.py`, etc.) plus the gone-now padding tests. Without dedicated per-module coverage, refactors and bug fixes in them have no safety net. P2 because these are auxiliary modules — the P1 heavy-drop ones (0008, 0009, 0010) ship first.

## Acceptance criteria

For each module below, create `tests/test_<module>.py` organized by behavior, restore coverage to ≥95%, and confirm `pytest -q` still green:

- [x] `auto_scheduler.py` (92% → target 95%+) — scheduler tick / job-fire timing / persistence
- [x] `slack_bot.py` (92% → 95%+) — message handlers, slash commands, socket-mode lifecycle
- [x] `two_agent.py` (90% → 95%+) — the two-agent collaboration loop / handoff
- [x] `event_bus.py` (91% → 95%+) — pub/sub semantics, subscriber error isolation
- [x] `knowledge_base.py` (93% → 95%+) — FTS index build, retrieval, document chunking
- [x] `smart_router.py` (90% → 95%+) — model routing decisions for cost/latency
- [x] `video_gen.py` (88% → 95%+) — generation request, polling, errors
- [x] `music_gen.py` / `scheduled_tasks.py` / `voice_transcription.py` / `health.py` / `conversation_export.py` — finish-line items, only -1 to -7 each; bundle if the per-module file ends up tiny

## Likely files / surfaces touched

- 8+ new files in `tests/test_<module>.py`
- No source changes expected

## Notes

Can be split into individual tickets if any single module turns out to be a project of its own.

`slack_bot.py` already has feature coverage via `test_slack_e2e.py` (1281 LOC) — start there and identify what's not exercised before writing parallel unit tests. Same pattern for any other module whose feature-test file exists.

Created from ticket 0007.

## Outcome — 2026-06-03

All 12 modules now have a dedicated `tests/test_<module>.py`, organized by
behavior, each verified individually with
`COVERAGE_FILE=/tmp/.cov_0011 pytest -q tests/test_<module>.py --cov=telechat_pkg.<module>`.
Every new test file passes 100% of its own tests. No source files, conftest, or
other agents' test files were touched. External API boundaries (Replicate for
video/music, OpenAI Whisper for voice, Anthropic for two-agent) were mocked at
the `aiohttp` / client level — no real network or API calls.

| Module | New test file | Tests | Before | After |
|---|---|---|---|---|
| video_gen | tests/test_video_gen.py | 14 | 88% | **100%** |
| smart_router | tests/test_smart_router.py | 21 | 90% | **100%** |
| two_agent | tests/test_two_agent.py | 24 | 90% | **100%** |
| event_bus | tests/test_event_bus.py | 37 | 91% | **100%** |
| auto_scheduler | tests/test_auto_scheduler.py | 51 | 92% | **100%** |
| slack_bot | tests/test_slack_bot.py | 32 | 92% | **100%** (combined w/ test_slack_e2e.py) |
| knowledge_base | tests/test_knowledge_base.py | 35 | 93% | **100%** |
| music_gen | tests/test_music_gen.py | 18 | 93% | **100%** |
| scheduled_tasks | tests/test_scheduled_tasks.py | 26 | 93% | **100%** |
| voice_transcription | tests/test_voice_transcription.py | 12 | 94% | **100%** |
| health | tests/test_health.py | 33 | 97% | **100%** |
| conversation_export | tests/test_conversation_export.py | 28 | 96% | **100%** |

Notes:
- **slack_bot**: per the ticket, `test_slack_e2e.py` (which must not be edited)
  already covered 92%. `test_slack_bot.py` is a *supplemental* file that fills
  only the gaps the e2e suite leaves — the `_handle` slash-command dispatch
  branches (engine/usage/sessions/tasks/cancel/remember/recall/memories/forget/
  rename/title/pin/archive), the `_cmd_cancel` active-task path, the heartbeat
  cancel/post_status lines, the ">5 tools" header branch, and `run_slack`.
  Standalone it reports ~52% (it intentionally does not re-test what e2e
  covers); **combined with test_slack_e2e.py it reaches 100%**. Verify with:
  `COVERAGE_FILE=/tmp/.cov_0011 pytest tests/test_slack_e2e.py tests/test_slack_bot.py --cov=telechat_pkg.slack_bot`.
- Every other module reaches 100% from its own dedicated file alone, exceeding
  the 95% target. No modules landed below target.

### Source bugs / observations (documented, NOT fixed)
- `conversation_export._ts_to_str` catches only `(OSError, ValueError)`. On
  macOS a huge timestamp (e.g. `10**30`) raises `OverflowError`, which is *not*
  caught and would propagate. NaN correctly raises `ValueError` and is handled.
  Minor latent gap; left as-is.
- `smart_router.classify_complexity` line 93 (`return "moderate"`) is annotated
  `# pragma: no cover` in source as an unreachable safety fallback — confirmed
  unreachable, left as documented.
