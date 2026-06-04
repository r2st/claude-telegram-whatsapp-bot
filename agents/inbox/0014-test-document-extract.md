---
id: 0014
title: Write behavior-organized tests for telechat_pkg/document_extract.py
role: builder
priority: P1
owner:
started:
status: inbox
depends_on: []
---

## Goal

Create `tests/test_document_extract.py` covering the document_extract module's real behaviors. Coverage dropped 100% → **59%** after ticket 0012 — 62 lines unexercised. Second-largest drop.

## Why it matters

document_extract is what turns user-uploaded PDFs / DOCXes into text the bot can reason about. Silent failures (extraction returns empty, encoding mangled, page boundaries lost) lead to the bot answering questions about a document it didn't actually read. Behavior-organized tests around format detection, extraction correctness, page-boundary preservation, and error reporting catch the failure modes users actually experience.

## Acceptance criteria

- [ ] `tests/test_document_extract.py` created, organized by behavior
- [ ] Test classes named for the extraction concern (e.g. `TestPdfExtraction`, `TestDocxExtraction`, `TestUnsupportedFormat`, `TestEncodingFallback`, `TestPageBoundaries`)
- [ ] Coverage of `telechat_pkg/document_extract.py` returns to ≥95%
- [ ] `pytest -q tests/test_document_extract.py` green
- [ ] Full `pytest -q` still green (modulo 3 pre-existing cassette failures from 0006)

## Likely files / surfaces touched

- `tests/test_document_extract.py` (new)
- May need a `tests/fixtures/` with sample PDF/DOCX files (small ones, no real data)
- No source changes expected

## Notes

PyMuPDF and python-docx are optional deps — verify the module's fallback behavior when those aren't installed (skip vs error vs degraded mode), since that's part of the user-facing behavior.

Created from ticket 0012.
