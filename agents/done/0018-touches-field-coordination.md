---
id: 0018
title: Add `touches:` field + overlap check to prevent multi-agent file collisions
role: builder
priority: P1
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
touches:
  - AGENTS.md
  - agents/README.md
  - agents/_template.md
  - agents/check-overlap.sh
  - docs/decisions/0002-touches-field-coordination.md
---

## Goal

Extend the ticketing protocol from ADR 0001 to prevent the next-most-likely collision: two agents claim *different* tickets that both modify the same file. Today's protocol catches same-ticket double-claims (via "one ticket per agent" + `git pull` before claiming) but says nothing about cross-ticket file overlap.

The mechanism: a structured `touches:` list in each ticket's frontmatter declaring which files the ticket will modify, plus a small shell script that an agent runs before claiming to check `agents/tasks/*.md` for path overlap.

## Why it matters

Concrete near-term collision risks once agents start claiming:

- `telegram_bot.py` (3527 LOC) — testing it (would-be 0017 from CODE_REVIEW P1 #14 split) AND adding new commands could land at the same time
- `web_chat.py` — multiple security findings in CODE_REVIEW P0 #3-#4 + P0 #6 all live here
- `store.py` / `memory.py` — P0 #1 (just verified fixed) showed how often these get touched
- `pyproject.toml` — any dep addition / extras change / pytest config (P2 #22) lands here

Without `touches:`, two agents would silently both branch off the same file state and overwrite each other at push. With `touches:`, the second agent sees the conflict at claim time and chooses: wait, coordinate via the ticket body, or pick a different ticket.

## Acceptance criteria

- [x] ADR `docs/decisions/0002-touches-field-coordination.md` written
- [x] `agents/_template.md` extended with `touches:` field in frontmatter (and the "Likely files / surfaces touched" prose section now defers to it)
- [x] `AGENTS.md` "Coordination rules" + "Ticket lifecycle" updated to reference the field and the pre-claim check (rules renumbered to 1–9; check step inserted)
- [x] `agents/README.md` "Claiming a ticket" section updated with the check step + three conflict-resolution responses
- [x] `agents/check-overlap.sh` created (executable, 69 LOC including portability shim — slightly larger than estimated, mostly comments and bash 3.2 compatibility)
- [x] Smoke test: synthetic ticket 0099 with overlapping touches → script reports 2 conflicting paths and exits 1 ✓
- [x] Smoke test: `agents/check-overlap.sh 0018` against current tasks/ → "no overlap with active claims. Safe to claim 0018." exit 0 ✓
- [x] Bonus smoke test: ticket without `touches:` field → script warns and exits 1 (no false-positive clean) ✓

**Descoped mid-ticket**: retrofitting existing inbox tickets with `touches:` (originally an acceptance criterion). Another agent moved 8 of those target tickets from inbox → tasks/ while this ticket was being written. Retrofitting is now a separate concern, tracked in Notes.

## Likely files / surfaces touched

See `touches:` above.

## Notes

Pre-existing tickets 0001–0004 (authored before this ticket) are deliberately not retrofitted — when an agent goes to claim one of those, AGENTS.md will tell them: "missing `touches:`? Add it before claiming, reflecting the modules you'll modify." That makes the field self-bootstrapping without me having to guess on someone else's scope.

Future-work explicitly out of scope:
- Glob patterns in `touches:` — start with exact paths; add globs if/when a ticket genuinely needs them
- Read-only declarations — file *reads* don't conflict and don't need to be listed
- Tooling that auto-extracts `touches:` from a diff — manual is fine at this scale
- Integration with a git hook — adds friction; the social-enforcement model from ADR 0001 still applies
- Retrofitting `touches:` onto pre-existing tickets (inbox and in-flight tasks). File a follow-up ticket once the in-flight tasks/ claims have settled, since editing tickets owned by another agent is itself a coordination question this ticket is too small to resolve.

**Live collision observed during this ticket** (validates the ADR): another agent ran in parallel and (1) reused ID 0017 for a new ticket while my 0017 was already committed at 828f934, (2) claimed 8 of the originally-listed retrofit-target tickets (0008–0011, 0013–0016) from inbox → tasks/, and (3) produced 20+ new `tests/test_<module>.py` files plus a flake-fix to `store.py` / `test_security.py`. Concrete demonstration of the gap this ADR addresses: had they run `agents/check-overlap.sh` against this ticket's original `touches:`, their claim of 0008 would have flagged a conflict with my listed retrofit. They didn't run it (it didn't exist yet — chicken-and-egg). Once shipped, future agents are expected to.

## Outcome — 2026-06-03

Shipped `touches:` frontmatter field + `agents/check-overlap.sh` pre-claim check + ADR 0002 + AGENTS.md / _template.md / README.md updates. Five files added/modified, no source-code changes. Script verified on bash 3.2 (macOS default) — initially used `mapfile` (bash 4+) and had to rewrite portably. Three smoke tests pass: clean candidate, two-path overlap conflict, missing-touches warning. Retrofit of pre-existing tickets descoped mid-flight when another agent claimed the retrofit-target tickets in parallel — left as future-work in Notes.
