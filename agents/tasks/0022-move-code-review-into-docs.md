---
id: 0022
title: Move CODE_REVIEW.md from repo root into docs/
role: builder
priority: P2
owner:
started:
status: inbox
depends_on: []
touches:
  - CODE_REVIEW.md
  - docs/CODE_REVIEW.md
  - AGENTS.md
---

## Goal

CODE_REVIEW.md sits at the repo root (507 lines after ticket 0020's annotations). Everything else of that flavor — `architecture.md`, `self-improving-design.md`, `advanced-telegram-features.md`, `implementation-tracker.md`, decision records — already lives in `docs/`. Move CODE_REVIEW.md into `docs/` so the root markdown is just `README.md`, `LICENSE`, `AGENTS.md` (the small set genuinely meant to be read first) and the long-form analyses are co-located under `docs/`.

## Why it matters

- **Discoverability:** `ls docs/` is currently incomplete — a casual look misses the security review entirely.
- **Root signal-to-noise:** A new contributor `ls`-ing the repo gets a 32 KB file with no immediate "what is this" context. README.md + LICENSE + AGENTS.md is the minimum every project at root benefits from; CODE_REVIEW belongs with its peers.
- **`.dockerignore` coverage:** the `.dockerignore` I added in 0021 already excludes `docs/`. Moving CODE_REVIEW.md there makes that exclusion automatically cover the review — one less file the future-fixed Dockerfile has to think about.

## Acceptance criteria

- [ ] `CODE_REVIEW.md` moved to `docs/CODE_REVIEW.md` via `git mv` (preserves blame/history)
- [ ] `AGENTS.md` "Where things live" table updated: `Code review notes | docs/CODE_REVIEW.md`
- [ ] No other in-tree references broken (verified: `grep -rn 'CODE_REVIEW.md' --include='*.md' --include='*.py' --include='*.sh' --include='*.toml' --include='Dockerfile' .` shows only annotations within the review itself + done tickets, which are immutable but accept stale links per protocol)
- [ ] `agents/check-overlap.sh 0022` reports clean

## Likely files / surfaces touched

See `touches:` above.

## Notes

**Not in scope:** reconciling AGENTS.md vs agents/README.md content overlap. They serve different audiences (AGENTS.md = first-read project guide; agents/README.md = local workflow inside agents/) and have only mild redundancy on the claim-flow. File a separate ticket if/when it becomes confusing — current overlap is acceptable.

**Done tickets reference `CODE_REVIEW.md` at the root.** Tickets 0007, 0012, 0017, 0019, 0020, 0021 all cite it. Those are immutable per protocol — the references will become stale but readable. The new path is one level deeper; a reader following the breadcrumb gets a 404 in their editor's quick-open. Acceptable cost for the cleaner root.

Annotations inside CODE_REVIEW.md (the `**[RESOLVED ...]**` markers from ticket 0020) reference ticket numbers and commit SHAs — no path references. The move doesn't invalidate them.
