---
id: 0020
title: Mark stale findings in CODE_REVIEW.md inline so future agents stop chasing fixed issues
role: builder
priority: P2
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
touches:
  - CODE_REVIEW.md
---

## Goal

CODE_REVIEW.md was performed 2026-05-20. Since then several findings have been fixed (some by tickets in this branch, some by work that predated the ticketing system). Future agents reading the review treat it as authoritative and waste cycles re-verifying or re-fixing items that are already done. Mark each known-stale finding inline with `**[RESOLVED YYYY-MM-DD by <ref>]**` (or `**[PARTIALLY RESOLVED...]**`, or `**[OUT OF DATE: <current value>]**`), and add a one-line banner at the top noting the review date.

## Why it matters

Two concrete examples already happened in this branch:

- I started ticket 0007-style cleanup for `session_manager.py` thinking it was dead code (per P0 #1). Spent time verifying before discovering the bug was fixed and the module is actively used. The "no callers" claim was 2 weeks out of date.
- Other agent's ticket 0019 explicitly says *"non-bugs also observed and intentionally left as-is documented here so they aren't re-reported"* — they're already maintaining a separate stale-list because the review's own claims have drifted.

Centralizing the staleness markers in the review itself, where the finding lives, prevents that drift.

## Acceptance criteria

- [x] Top-of-file banner added (line 6): notes review date + current version (1.2.0) + the marker convention
- [x] Annotated 5 fully-resolved findings with `**[RESOLVED 2026-06-03]**` or `**[RESOLVED 2026-06-03 by ticket 0017]**`:
  - P0 #1 (session_manager.py history table) — line 48
  - §6 HIGH main.py 1083 LOC + QR encoder — line 296 (covers QR encoder claim and the LOC claim in one marker)
  - P1 #16 (optional deps not declared) — line 378
  - §8 MEDIUM scripts/* glob in pyproject — line 388
  - §8 LOW version drift (1.1.5 / 1.6.0) — line 393
  - P2 #19 (QR encoder dedup in suggested-improvements) — line 490 (separate marker because finding is restated in §10)
- [x] Annotated 1 partially-resolved finding: P1 #12 (MCP allowlist exists; env/PATH gap per 0019) — line 471
- [x] Annotated 1 number-update finding: §6 HIGH telegram_bot 3527 → 3604 LOC — line 290 (split itself still pending, no ticket yet)
- [x] Version "1.1.5" claim in header handled via top banner (avoids fragmenting the same fact across two markers)
- [x] No other findings touched — the rest are presumed-still-valid
- [x] `agents/check-overlap.sh 0020` reported "no overlap. Safe to claim 0020." (only ticket touching CODE_REVIEW.md)

## Likely files / surfaces touched

See `touches:` above.

## Notes

Annotation policy choices and why:

- **Inline at the finding** beats a separate "RESOLVED" file. Readers see the status where they see the finding; no separate document to consult; can't drift independently. Cost: the review file grows; trade is worth it given the file is reference material.
- **`**[RESOLVED YYYY-MM-DD by <ref>]**`** as the marker. Bold + bracketed = visually distinct from review prose. `<ref>` is the ticket NNNN or commit SHA so a reader can git log to the fix.
- **Don't strikethrough the original text.** Strikethrough makes the original finding hard to read and obscures what was actually originally found. The original wording is historical evidence — preserve it.
- **No top-level "✓ X items fixed" summary.** Counts drift; per-item annotations don't.
- **Don't audit beyond confirmed staleness.** This ticket isn't a full review refresh — that's a much bigger effort. Just mark what's known-stale with hard evidence (file inspection, commit references). Other findings stay un-annotated and are presumed still valid.

## Outcome — 2026-06-03

Added 9 markers to CODE_REVIEW.md (1 top-of-file banner + 8 inline annotations). 5 RESOLVED, 1 PARTIALLY RESOLVED, 1 NUMBER UPDATED, 1 cross-reference to RESOLVED in suggested-improvements (#19 restated). Each marker cites the ticket NNNN, commit SHA, or specific file:line so readers can audit the fix. CODE_REVIEW.md grew 489 → 507 lines (+18). No CODE_REVIEW prose deleted — original findings preserved as historical evidence under each new marker. Verified all annotations are findable via `grep -nE "\*\*\[RESOLVED 2026-06-03|\*\*\[PARTIALLY RESOLVED|\*\*\[NUMBER UPDATED" CODE_REVIEW.md`.
