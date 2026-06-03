---
id: NNNN
title: One-line description of what this ticket delivers
role: builder        # builder | reviewer | researcher | pitch
priority: P0         # P0 (demo-critical) | P1 (important) | P2 (nice-to-have)
owner:               # leave blank until claimed
started:             # YYYY-MM-DD, set when claimed
status: inbox        # inbox | in-progress | blocked | done
depends_on: []       # list of ticket IDs that must finish first
---

## Goal

What problem does this solve? Why now? Keep to 2–4 sentences.

## Why it matters

User-facing impact, demo relevance, or risk it mitigates. If you can't
justify this in 1–2 lines, the ticket may not be worth doing.

## Acceptance criteria

- [ ] Specific, testable outcome 1
- [ ] Specific, testable outcome 2
- [ ] Tests added/updated; `pytest -q` green
- [ ] Docs / implementation-tracker.md updated if relevant

## Likely files / surfaces touched

- `telechat_pkg/<module>.py`
- `tests/test_<module>.py`
- `docs/<doc>.md` (if behavior changes)

## Notes

Context, links, prior art, design constraints. Anything the next agent
would want to know before starting.

<!-- Outcome section (added when moving to done/) -->
<!--
## Outcome — YYYY-MM-DD

What shipped, where the code lives, and the test evidence (e.g.
`pytest -q tests/test_foo.py` 12 passed). 2–3 lines max.
-->
