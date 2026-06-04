---
id: 0017
title: Delete dead QR helper duplicates from main.py
role: builder
priority: P2
owner: claude-opus-4-7
started: 2026-06-03
status: done
depends_on: []
---

## Goal

Remove three dead-code function definitions from `telechat_pkg/main.py` that duplicate `qr_util.py` and are never called locally: `_get_local_ip`, `_print_web_qr`, `_render_qr_terminal`. The actual call site (`main.py:577`) already imports `print_web_qr` from `qr_util`, so the local copies are orphaned — likely artifacts of the qr_util.py extraction that didn't fully clean up.

## Why it matters

`CODE_REVIEW.md` P2 #19 originally flagged "~250 lines of hand-rolled QR + Reed–Solomon" in main.py duplicating qr_util.py. That big-ticket dedup is **already done** (per qr_util.py docstring: *"we used to ship a hand-rolled Reed–Solomon encoder here…so it was removed"*). What's left is a smaller tail: three duplicated helper functions (~60 LOC) sitting in main.py with no callers. Removing them: shrinks main.py (P1 #14 god-module concern), eliminates one of the "two parallel copies" maintainability risks, and finishes P2 #19.

## Acceptance criteria

- [x] Verified: `_get_local_ip`, `_print_web_qr`, `_render_qr_terminal` in `main.py` had zero callers outside their own definitions
- [x] All three function definitions removed from `main.py`
- [x] `from .qr_util import print_web_qr` at `main.py:576` retained (line number shifted from 577 → 576 after the deletion)
- [x] `pytest -q` green: 2020 passed, 3 pre-existing cassette failures from 0006
- [x] `main.py` LOC: 906 → 844 (-62 lines)

## Likely files / surfaces touched

- `telechat_pkg/main.py` (3 function deletions)
- No new imports, no behavior change

## Notes

Closes the duplicate-implementation half of CODE_REVIEW.md P2 #19; the bigger Reed–Solomon dedup was already completed before this ticket existed. Marking P2 #19 fully done in any future stale-findings audit (which would itself be a separate ticket).

main.py was 1083 lines per CODE_REVIEW.md (May 2026); pre-ticket was 906. After this ticket: 844. Real progress toward sane size, though splitting per P1 #14 is still the bigger lever.

## Outcome — 2026-06-03

Deleted three dead-code helpers from `telechat_pkg/main.py`: `_get_local_ip` (13 LOC), `_print_web_qr` (29 LOC), `_render_qr_terminal` (17 LOC). All three were exact duplicates of functions in `qr_util.py`; the real call site at `main.py:577` was already using the imported `print_web_qr`, so the local copies were orphaned. main.py dropped 906 → 844 LOC (-62). `pytest -q` still passes 2020 tests. Closes the remaining tail of CODE_REVIEW.md P2 #19 — the bigger Reed–Solomon dedup was already done before this ticket.
