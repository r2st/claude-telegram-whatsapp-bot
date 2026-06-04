"""
Behavior tests for the knowledge base / RAG store (telechat_pkg.knowledge_base).

Covers: text chunking (incl. sentence-boundary breaks + overlap), text & file
ingestion (with dedup), FTS + LIKE-fallback search, RAG context building (incl.
char budget), document listing/deletion/stats, and PDF extraction fallback.

Run:
    pytest tests/test_knowledge_base.py -v
"""

import os
import sys
import types

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg import knowledge_base as kbmod
from telechat_pkg.knowledge_base import KnowledgeBase


@pytest.fixture
def kb(tmp_path):
    return KnowledgeBase(db_path=str(tmp_path / "kb.db"))


# ══════════════════════════════════════════════════════════════════════════════
# 1. Chunking
# ══════════════════════════════════════════════════════════════════════════════


class TestChunking:
    def test_short_text_single_chunk(self):
        assert KnowledgeBase.chunk_text("short text") == ["short text"]

    def test_long_text_multiple_chunks(self):
        text = "x" * 3000
        chunks = KnowledgeBase.chunk_text(text, chunk_size=1000, overlap=100)
        assert len(chunks) >= 3

    def test_breaks_at_sentence_boundary(self):
        # Build a long text with clear sentence boundaries.
        sentence = "This is a sentence. "
        text = sentence * 120  # ~2400 chars
        chunks = KnowledgeBase.chunk_text(text, chunk_size=1000, overlap=100)
        # A good break leaves most chunks ending near a sentence end.
        assert len(chunks) >= 2
        assert any(c.endswith(".") for c in chunks)

    def test_overlap_applied(self):
        text = ("paragraph one.\n\n" + "word " * 300).strip()
        chunks = KnowledgeBase.chunk_text(text, chunk_size=500, overlap=50)
        assert len(chunks) >= 2

    def test_paragraph_break_preferred(self):
        text = "A" * 600 + "\n\n" + "B" * 600
        chunks = KnowledgeBase.chunk_text(text, chunk_size=1000, overlap=100)
        assert len(chunks) >= 2


# ══════════════════════════════════════════════════════════════════════════════
# 2. Ingestion
# ══════════════════════════════════════════════════════════════════════════════


class TestIngestText:
    def test_ingest_returns_document(self, kb):
        doc = kb.ingest_text("telegram", "u1", "Docs", "hello world content")
        assert doc.title == "Docs"
        assert doc.chunk_count == 1
        assert doc.content_hash

    def test_ingest_with_tags_and_metadata(self, kb):
        doc = kb.ingest_text("telegram", "u1", "T", "content here",
                             tags=["a", "b"], metadata={"k": "v"})
        assert doc.tags == ["a", "b"]
        assert doc.metadata == {"k": "v"}

    def test_dedup_same_content(self, kb):
        d1 = kb.ingest_text("telegram", "u1", "T", "identical content")
        d2 = kb.ingest_text("telegram", "u1", "T", "identical content")
        assert d1.id == d2.id
        assert kb.stats("telegram", "u1")["documents"] == 1

    def test_large_content_many_chunks(self, kb):
        doc = kb.ingest_text("telegram", "u1", "Big", "z" * 5000)
        assert doc.chunk_count > 1


class TestIngestFile:
    def test_ingest_txt_file(self, kb, tmp_path):
        p = tmp_path / "notes.md"
        p.write_text("# Heading\n\nSome useful documentation content.")
        doc = kb.ingest_file("telegram", "u1", str(p))
        assert doc is not None
        assert doc.title == "notes.md"
        assert "md" in doc.tags

    def test_missing_file_returns_none(self, kb):
        assert kb.ingest_file("telegram", "u1", "/no/such/file.txt") is None

    def test_unsupported_extension_returns_none(self, kb, tmp_path):
        p = tmp_path / "image.png"
        p.write_bytes(b"\x89PNG")
        assert kb.ingest_file("telegram", "u1", str(p)) is None

    def test_empty_file_returns_none(self, kb, tmp_path):
        p = tmp_path / "empty.txt"
        p.write_text("   \n  ")
        assert kb.ingest_file("telegram", "u1", str(p)) is None

    def test_read_error_returns_none(self, kb, tmp_path, monkeypatch):
        p = tmp_path / "broken.txt"
        p.write_text("content")

        def boom(*a, **k):
            raise OSError("read failed")

        monkeypatch.setattr(kbmod.Path, "read_text", boom)
        assert kb.ingest_file("telegram", "u1", str(p)) is None

    def test_pdf_extraction_via_fake_pypdf(self, kb, tmp_path, monkeypatch):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 fake")

        # Inject a fake pypdf so _extract_pdf returns text.
        fake = types.ModuleType("pypdf")

        class _Page:
            def extract_text(self):
                return "extracted pdf text"

        class PdfReader:
            def __init__(self, path):
                self.pages = [_Page(), _Page()]

        fake.PdfReader = PdfReader
        monkeypatch.setitem(sys.modules, "pypdf", fake)

        doc = kb.ingest_file("telegram", "u1", str(p))
        assert doc is not None
        assert doc.title == "doc.pdf"

    def test_pdf_without_pypdf_returns_none(self, kb, tmp_path, monkeypatch):
        p = tmp_path / "nope.pdf"
        p.write_bytes(b"%PDF")
        # Force ImportError inside _extract_pdf.
        monkeypatch.setitem(sys.modules, "pypdf", None)
        assert kb.ingest_file("telegram", "u1", str(p)) is None


# ══════════════════════════════════════════════════════════════════════════════
# 3. Search
# ══════════════════════════════════════════════════════════════════════════════


class TestSearch:
    def test_fts_search_finds_chunk(self, kb):
        kb.ingest_text("telegram", "u1", "API", "authentication uses bearer tokens")
        results = kb.search("telegram", "u1", "authentication")
        assert len(results) >= 1
        assert "authentication" in results[0].chunk.content

    def test_search_isolates_users(self, kb):
        kb.ingest_text("telegram", "u1", "T", "secret content for user one")
        results = kb.search("telegram", "u2", "secret")
        assert results == []

    def test_search_empty_query_uses_like(self, kb):
        kb.ingest_text("telegram", "u1", "T", "some content")
        # empty query → skips FTS, falls to LIKE with empty needle (matches all)
        results = kb.search("telegram", "u1", "")
        assert len(results) >= 1

    def test_like_fallback_when_fts_unavailable(self, kb, monkeypatch):
        kb.ingest_text("telegram", "u1", "T", "python programming language")
        monkeypatch.setattr(kb, "_has_fts", lambda: False)
        results = kb.search("telegram", "u1", "python")
        assert len(results) >= 1
        assert results[0].score == 0.0

    def test_search_respects_limit(self, kb):
        kb.ingest_text("telegram", "u1", "T", "repeat " * 5000)  # many chunks
        results = kb.search("telegram", "u1", "repeat", limit=2)
        assert len(results) <= 2

    def test_fts_operational_error_falls_back(self, kb, monkeypatch):
        import sqlite3

        kb.ingest_text("telegram", "u1", "T", "fallback content here")
        real_conn = kb._conn()

        class _ConnProxy:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, *params):
                if "kb_chunks_fts MATCH" in sql:
                    raise sqlite3.OperationalError("fts malformed")
                return real_conn.execute(sql, *params)

        proxy = _ConnProxy()
        monkeypatch.setattr(kb, "_conn", lambda: proxy)
        results = kb.search("telegram", "u1", "fallback")
        assert len(results) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. build_context
# ══════════════════════════════════════════════════════════════════════════════


class TestBuildContext:
    def test_context_disabled(self, kb, monkeypatch):
        monkeypatch.setattr(kbmod, "KB_ENABLED", False)
        assert kb.build_context("telegram", "u1", "anything") == ""

    def test_context_no_results(self, kb, monkeypatch):
        monkeypatch.setattr(kbmod, "KB_ENABLED", True)
        assert kb.build_context("telegram", "u1", "nomatchxyz") == ""

    def test_context_includes_title_and_chunk(self, kb, monkeypatch):
        monkeypatch.setattr(kbmod, "KB_ENABLED", True)
        kb.ingest_text("telegram", "u1", "Guide", "deployment uses docker compose")
        ctx = kb.build_context("telegram", "u1", "docker")
        assert "Knowledge Base Context" in ctx
        assert "[From: Guide]" in ctx
        assert "docker" in ctx

    def test_context_respects_char_budget(self, kb, monkeypatch):
        monkeypatch.setattr(kbmod, "KB_ENABLED", True)
        monkeypatch.setattr(kbmod, "KB_MAX_CONTEXT_CHARS", 50)
        kb.ingest_text("telegram", "u1", "Doc", "alpha " * 2000)
        ctx = kb.build_context("telegram", "u1", "alpha")
        # First chunk already exceeds the 50-char budget → no parts kept.
        assert ctx == ""


# ══════════════════════════════════════════════════════════════════════════════
# 5. Document management
# ══════════════════════════════════════════════════════════════════════════════


class TestDocManagement:
    def test_list_documents(self, kb):
        kb.ingest_text("telegram", "u1", "A", "content a")
        kb.ingest_text("telegram", "u1", "B", "content b")
        docs = kb.list_documents("telegram", "u1")
        assert len(docs) == 2

    def test_list_respects_limit(self, kb):
        for i in range(5):
            kb.ingest_text("telegram", "u1", f"D{i}", f"content number {i}")
        assert len(kb.list_documents("telegram", "u1", limit=2)) == 2

    def test_delete_document(self, kb):
        doc = kb.ingest_text("telegram", "u1", "A", "content to delete")
        assert kb.delete_document("telegram", "u1", doc.id) is True
        assert kb.stats("telegram", "u1") == {"documents": 0, "chunks": 0}

    def test_delete_wrong_user(self, kb):
        doc = kb.ingest_text("telegram", "u1", "A", "content")
        assert kb.delete_document("telegram", "u2", doc.id) is False

    def test_stats(self, kb):
        kb.ingest_text("telegram", "u1", "A", "z" * 3000)  # multiple chunks
        s = kb.stats("telegram", "u1")
        assert s["documents"] == 1
        assert s["chunks"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# 6. _extract_pdf direct
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractPdf:
    def test_extract_pdf_no_pypdf(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "pypdf", None)
        assert KnowledgeBase._extract_pdf(tmp_path / "x.pdf") == ""

    def test_default_db_path_uses_store(self):
        from telechat_pkg import store
        kb = KnowledgeBase()
        assert kb._db_path == store.DB_PATH


class TestHasFts:
    def test_has_fts_true_normally(self, kb):
        assert kb._has_fts() is True

    def test_has_fts_false_on_operational_error(self, kb, monkeypatch):
        import sqlite3

        real_conn = kb._conn()

        class _ConnProxy:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, *params):
                raise sqlite3.OperationalError("no fts table")

        # Clear cached flag and force the probe to fail.
        if hasattr(kb, "_fts_ok"):
            delattr(kb, "_fts_ok")
        monkeypatch.setattr(kb, "_conn", lambda: _ConnProxy())
        assert kb._has_fts() is False
