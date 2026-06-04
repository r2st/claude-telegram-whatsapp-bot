---
id: 0014
title: Write behavior-organized tests for telechat_pkg/document_extract.py
role: builder
priority: P1
owner: claude-opus-4-8
started: 2026-06-03
status: done
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

## Outcome — 2026-06-03

- File: `tests/test_document_extract.py` (new). No fixture files created — DOCX
  samples are generated on the fly via python-docx, PDFs via an injected fake
  `fitz` module, so `tests/fixtures/` was not needed.
- Tests: **59**, all green.
- Coverage: `telechat_pkg/document_extract.py` **59% → 99%** (153 stmts, 2 miss).
- Verify command: `COVERAGE_FILE=/tmp/.cov_0014 python -m pytest -q
  tests/test_document_extract.py --cov=telechat_pkg.document_extract
  --cov-report=term-missing` → 59 passed.

### Environment dep status
- PyMuPDF (`fitz`): **NOT installed**. The genuine not-installed path is tested
  directly (returns an `ExtractResult` with an error string, never raises).
  Real extraction is tested by injecting a fake `fitz` into `sys.modules`.
- python-docx (`docx`): **installed**. Real `.docx` files are built and parsed.
  The not-installed path is simulated by hiding `docx` via monkeypatch.

### Uncovered lines (left + documented)
- Lines **247-248**: the `except Exception` inside `extract()`'s
  unknown-extension fallback. Dead code — `extract_text_file()` catches all
  exceptions internally and always returns an `ExtractResult`, so the outer
  `try: return extract_text_file(path)` can never raise. Documented in the test
  file; not contorted into coverage.

### Behavior notes / non-bugs observed
- `extract()`'s "unsupported format" branch never actually reports an
  unsupported format — it always falls back to reading the file as text
  (errors="replace"), so even binary/unknown files decode to mojibake text with
  no error. This is by design but means truly unsupported binaries are returned
  as garbled text rather than rejected. Not a crash; documented, not changed.
- The unknown-format error path (lines 247-248) is therefore unreachable.

No source bugs found. No source files modified.
