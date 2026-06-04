---
id: 0012
title: Delete tests/test_security_full.py (4781 LOC of stacked padding)
role: builder
priority: P1
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Delete `tests/test_security_full.py` wholesale. Its docstring openly admits it exists *"to achieve 100% combined coverage with test_security.py"* — i.e. it complements the real security test file (1544-LOC `test_security.py`) by mechanically covering remaining lines. Class names confirm: three stacked passes — `*Full` (original), `*Additional` / `*FinalCoverage` (second pass), `LastMile*` (third pass). 4781 LOC / 374 tests / 467 assertions, but assertions are mostly shape-checks ("did the thing run?") not behavior verifications.

## Why it matters

The padding methodology is even more egregious than the 9 files removed in ticket 0007. Class names like `MemoryFTSOperationalError`, `LinkUnderstandingNonHttpScheme`, `StoreWriteQueueRace`, `WebChatWSError` are named after the *specific exception or branch* they hit — line-pegged at the highest resolution possible. Tests like `test_enqueue_write_full_fallback` have no assertion at all — they fill a queue and call a function to exercise the "queue full" branch, with no check that the fallback did the right thing. Worst smell: `test_db_writer_batches` is `skipif CI != "true"` with comment *"flaky in full-suite due to shared state; passes in isolation"* — a written admission that the test is fragile coverage furniture.

## Acceptance criteria

- [x] Coverage baseline captured against current main (post-0007 state): 82% total
- [x] `tests/test_security_full.py` deleted (4781 LOC, 374 tests)
- [x] Coverage delta computed; per-module drops recorded in outcome
- [x] 4 follow-up tickets filed (0013/0014/0015 P1, 0016 P2). Cross-checked against 0008–0011 — no overlap.
- [x] `pytest -q` green: 2020 passed, 3 failed (the 3 pre-existing cassette failures from 0006)
- [x] No remaining tests use `LastMile*` / `*FinalCoverage` / `*Additional` / branch-pegged class naming (verified: `grep -rn 'class.*LastMile\|class.*FinalCoverage' tests/` returns 0 matches)

## Likely files / surfaces touched

- 1 deletion in `tests/`
- Possibly 1–N new tickets in `agents/inbox/` for modules whose coverage drops without a dedicated test file
- No source changes

## Notes

The 17 modules exercised by this file (per class names): web_fetch, web_search, link_understanding, markdown_v2, text_chunking, document_extract, error_classifier, store, memory, cost_budget, feedback, coder, resource_limiter, mcp_client, web_chat, browser_automation, session_manager.

These don't overlap with the 0011 umbrella set (auto_scheduler, slack_bot, two_agent, event_bus, knowledge_base, smart_router, video_gen, music_gen, scheduled_tasks, voice_transcription, health, conversation_export) — so coverage drops surfaced here will need their own tickets.

Out of scope: writing the real per-module tests for the modules that drop. As with 0007, those go into follow-up tickets so each can be claimed independently.

## Outcome — 2026-06-03

Deleted `tests/test_security_full.py` (4781 LOC / 374 tests / 467 assertions). The file's own docstring confirmed the verdict: *"Security tests — full coverage complement. Covers every uncovered line in the 17 security-relevant modules to achieve 100% combined coverage with test_security.py."* — explicitly line-pegged. Three stacked padding tiers in the class naming (`*Full`, `*Additional`/`*FinalCoverage`, `LastMile*`) plus per-branch class names (`MemoryFTSOperationalError`, `LinkUnderstandingNonHttpScheme`, `StoreWriteQueueRace`, `WebChatWSError`) were the giveaway.

Suite impact: 2393 passing → 2020 passing (-373). Wall time 59s → 42s (-29%). Total coverage 82% → 79% (-3 pts).

Per-module coverage drops from this ticket:

| Module | Before (0007 outcome) | After 0012 | Δ |
|---|---|---|---|
| `resource_limiter.py` | 100% | 51% | **-49** |
| `document_extract.py` | 100% | 59% | **-41** |
| `mcp_client.py` | 96% | 61% | **-35** |
| `browser_automation.py` | 100% | 90% | -10 |
| `memory.py` | 100% | 92% | -8 |
| `web_fetch.py` | 100% | 95% | -5 |
| `cost_budget.py` | 100% | 96% | -4 |
| `store.py` | 96% | 92% | -4 |
| `web_search.py` | 100% | 97% | -3 |
| `link_understanding.py` | 100% | 97% | -3 |
| `markdown_v2.py` | 100% | 98% | -2 |
| `text_chunking.py` | 100% | 98% | -2 |
| `web_chat.py` | 94% | 93% | -1 |
| Others | unchanged | unchanged | 0 |

Filed follow-up tickets:

- **0013** test_resource_limiter.py (P1, -49 pts, security-relevant per CODE_REVIEW)
- **0014** test_document_extract.py (P1, -41 pts)
- **0015** test_mcp_client.py (P1, -35 pts, also cross-references CODE_REVIEW HIGH on subprocess exec)
- **0016** umbrella for the 4 moderate drops: browser_automation, memory, store, cost_budget (P2)

`web_fetch`, `web_search`, `link_understanding`, `markdown_v2`, `text_chunking`, `web_chat` left as-is — drops within acceptable noise (≤5 pts) and they have substantial coverage from `tests/test_security.py` (the real behavioral file) and feature-area tests.

Combined impact of tickets 0007 + 0012: removed 10 padding files / 1050 tests / 14,592 LOC. Suite now runs in 42s instead of 82s (50% faster). Coverage went 88% → 79% — a 9-point drop spread across 16+ modules that all need real per-module test files (tracked in 0008–0011, 0013–0016).
