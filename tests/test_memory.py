"""
Comprehensive tests for MemoryStore — edge cases, concurrency, FTS,
special characters, and data integrity.

Run:
    pytest tests/test_memory.py -v
"""

import os
import sqlite3
import tempfile
import threading
import time
import unittest.mock

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-api-key")

from telechat_pkg.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "test_memory.db"))


# ══════════════════════════════════════════════════════════════════════════════
# 1. Basic CRUD
# ══════════════════════════════════════════════════════════════════════════════


class TestBasicCRUD:
    def test_remember_returns_memory(self, store):
        mem = store.remember("tg", "u1", "likes python")
        assert mem.id
        assert mem.platform == "tg"
        assert mem.user_id == "u1"
        assert mem.content == "likes python"
        assert mem.importance == 0.5
        assert mem.created_at > 0
        assert mem.updated_at > 0

    def test_remember_strips_whitespace(self, store):
        mem = store.remember("tg", "u1", "  padded content  \n")
        assert mem.content == "padded content"

    def test_recall_fts(self, store):
        store.remember("tg", "u1", "user prefers dark mode")
        results = store.recall("tg", "u1", "dark mode")
        assert len(results) >= 1
        assert any("dark" in r.content for r in results)

    def test_recall_empty_query_returns_all(self, store):
        store.remember("tg", "u1", "first")
        store.remember("tg", "u1", "second")
        results = store.recall("tg", "u1", "")
        assert len(results) >= 2

    def test_recall_no_results(self, store):
        results = store.recall("tg", "u1", "nonexistent_xyz_123")
        assert results == []

    def test_forget_returns_true(self, store):
        mem = store.remember("tg", "u1", "to forget")
        assert store.forget("tg", "u1", mem.id) is True

    def test_forget_nonexistent_returns_false(self, store):
        assert store.forget("tg", "u1", "no-such-id") is False

    def test_forget_wrong_platform(self, store):
        mem = store.remember("tg", "u1", "platform test")
        assert store.forget("slack", "u1", mem.id) is False

    def test_forget_wrong_user(self, store):
        mem = store.remember("tg", "u1", "user test")
        assert store.forget("tg", "u2", mem.id) is False

    def test_update_content(self, store):
        mem = store.remember("tg", "u1", "original")
        updated = store.update("tg", "u1", mem.id, content="changed")
        assert updated.content == "changed"
        assert updated.updated_at >= mem.updated_at

    def test_update_tags(self, store):
        mem = store.remember("tg", "u1", "tagged", tags=["a"])
        updated = store.update("tg", "u1", mem.id, tags=["b", "c"])
        assert updated.tags == ["b", "c"]

    def test_update_importance(self, store):
        mem = store.remember("tg", "u1", "imp test")
        updated = store.update("tg", "u1", mem.id, importance=0.9)
        assert updated.importance == 0.9

    def test_update_nonexistent(self, store):
        assert store.update("tg", "u1", "no-id", content="x") is None

    def test_list_memories(self, store):
        store.remember("tg", "u1", "mem a")
        store.remember("tg", "u1", "mem b")
        mems = store.list_memories("tg", "u1")
        assert len(mems) == 2

    def test_list_respects_limit(self, store):
        for i in range(10):
            store.remember("tg", "u1", f"mem {i}")
        mems = store.list_memories("tg", "u1", limit=3)
        assert len(mems) == 3

    def test_stats(self, store):
        store.remember("tg", "u1", "stat a")
        store.remember("tg", "u1", "stat b")
        s = store.stats("tg", "u1")
        assert s["total"] == 2
        assert s["oldest"] is not None
        assert s["newest"] is not None
        assert s["newest"] >= s["oldest"]

    def test_stats_empty_user(self, store):
        s = store.stats("tg", "nobody")
        assert s["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Importance clamping
# ══════════════════════════════════════════════════════════════════════════════


class TestImportance:
    def test_clamp_high(self, store):
        mem = store.remember("tg", "u1", "high", importance=5.0)
        assert mem.importance == 1.0

    def test_clamp_low(self, store):
        mem = store.remember("tg", "u1", "low", importance=-3.0)
        assert mem.importance == 0.0

    def test_clamp_zero(self, store):
        mem = store.remember("tg", "u1", "zero", importance=0.0)
        assert mem.importance == 0.0

    def test_clamp_one(self, store):
        mem = store.remember("tg", "u1", "one", importance=1.0)
        assert mem.importance == 1.0

    def test_update_clamp_high(self, store):
        mem = store.remember("tg", "u1", "x")
        updated = store.update("tg", "u1", mem.id, importance=99.0)
        assert updated.importance == 1.0

    def test_update_clamp_low(self, store):
        mem = store.remember("tg", "u1", "x")
        updated = store.update("tg", "u1", mem.id, importance=-99.0)
        assert updated.importance == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tag filtering
# ══════════════════════════════════════════════════════════════════════════════


class TestTags:
    def test_recall_with_matching_tag(self, store):
        store.remember("tg", "u1", "tag match", tags=["pref"])
        store.remember("tg", "u1", "no tag match")
        results = store.recall("tg", "u1", "match", tags=["pref"])
        assert all("pref" in r.tags for r in results)

    def test_recall_with_nonmatching_tag(self, store):
        store.remember("tg", "u1", "only tagged", tags=["x"])
        results = store.recall("tg", "u1", "tagged", tags=["y"])
        assert len(results) == 0

    def test_list_with_tag_filter(self, store):
        store.remember("tg", "u1", "a", tags=["work"])
        store.remember("tg", "u1", "b", tags=["personal"])
        mems = store.list_memories("tg", "u1", tags=["work"])
        assert len(mems) == 1
        assert mems[0].tags == ["work"]

    def test_empty_tags_stored_as_list(self, store):
        mem = store.remember("tg", "u1", "no tags")
        assert mem.tags == []

    def test_multiple_tags(self, store):
        mem = store.remember("tg", "u1", "multi", tags=["a", "b", "c"])
        assert mem.tags == ["a", "b", "c"]


# ══════════════════════════════════════════════════════════════════════════════
# 4. Platform / user isolation
# ══════════════════════════════════════════════════════════════════════════════


class TestIsolation:
    def test_different_platforms_isolated(self, store):
        store.remember("telegram", "u1", "tg memory")
        store.remember("slack", "u1", "slack memory")
        tg = store.list_memories("telegram", "u1")
        sl = store.list_memories("slack", "u1")
        assert len(tg) == 1
        assert len(sl) == 1
        assert tg[0].content == "tg memory"
        assert sl[0].content == "slack memory"

    def test_different_users_isolated(self, store):
        store.remember("tg", "alice", "alice mem")
        store.remember("tg", "bob", "bob mem")
        assert len(store.list_memories("tg", "alice")) == 1
        assert len(store.list_memories("tg", "bob")) == 1

    def test_recall_only_own_platform(self, store):
        store.remember("tg", "u1", "telegram specific content")
        results = store.recall("slack", "u1", "telegram specific")
        assert len(results) == 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Special characters and edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSpecialContent:
    def test_unicode_content(self, store):
        mem = store.remember("tg", "u1", "loves sushi")
        assert mem.content == "loves sushi"

    def test_quotes_in_content(self, store):
        mem = store.remember("tg", "u1", 'said "hello world"')
        assert '"hello' in mem.content

    def test_single_quotes(self, store):
        mem = store.remember("tg", "u1", "it's fine")
        assert mem.content == "it's fine"

    def test_newlines_in_content(self, store):
        mem = store.remember("tg", "u1", "line1\nline2\nline3")
        assert "\n" in mem.content

    def test_sql_injection_attempt(self, store):
        mem = store.remember("tg", "u1", "'; DROP TABLE memories; --")
        assert mem.content == "'; DROP TABLE memories; --"
        mems = store.list_memories("tg", "u1")
        assert len(mems) >= 1

    def test_html_in_content(self, store):
        mem = store.remember("tg", "u1", "<script>alert('xss')</script>")
        assert "<script>" in mem.content

    def test_very_long_content(self, store):
        long_text = "x" * 10000
        mem = store.remember("tg", "u1", long_text)
        assert len(mem.content) == 10000

    def test_empty_string_content(self, store):
        mem = store.remember("tg", "u1", "")
        assert mem.content == ""

    def test_whitespace_only_content(self, store):
        mem = store.remember("tg", "u1", "   ")
        assert mem.content == ""

    def test_special_fts_chars_in_query(self, store):
        store.remember("tg", "u1", "regular content here")
        results = store.recall("tg", "u1", "content AND here OR (not)")
        # Should not crash — FTS query gets quoted

    def test_tags_with_special_chars(self, store):
        mem = store.remember("tg", "u1", "tag test", tags=["a:b", "c/d", "e f"])
        assert mem.tags == ["a:b", "c/d", "e f"]


# ══════════════════════════════════════════════════════════════════════════════
# 6. FTS search quality
# ══════════════════════════════════════════════════════════════════════════════


class TestFTSSearch:
    def test_stemming_matches(self, store):
        store.remember("tg", "u1", "user is running fast")
        results = store.recall("tg", "u1", "run")
        assert len(results) >= 1

    def test_partial_word_match(self, store):
        store.remember("tg", "u1", "programming in python")
        results = store.recall("tg", "u1", "python")
        assert len(results) >= 1

    def test_multiple_word_query(self, store):
        store.remember("tg", "u1", "prefers dark mode with blue accents")
        results = store.recall("tg", "u1", "dark blue")
        assert len(results) >= 1

    def test_recall_respects_limit(self, store):
        for i in range(20):
            store.remember("tg", "u1", f"item number {i}")
        results = store.recall("tg", "u1", "item", limit=5)
        assert len(results) <= 5

    def test_fts_query_quoting(self):
        assert MemoryStore._to_fts_query("hello world") == '"hello" "world"'
        assert MemoryStore._to_fts_query("  spaces  ") == '"spaces"'
        assert MemoryStore._to_fts_query("") == '""'

    def test_fts_query_double_quotes(self):
        result = MemoryStore._to_fts_query('say "hi"')
        assert '""' in result  # quotes get doubled


# ══════════════════════════════════════════════════════════════════════════════
# 7. Concurrency
# ══════════════════════════════════════════════════════════════════════════════


class TestConcurrency:
    def test_concurrent_writes(self, store):
        errors = []

        def writer(platform, uid, n):
            try:
                for i in range(20):
                    store.remember(platform, uid, f"concurrent write {n}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("tg", "u1", i))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        mems = store.list_memories("tg", "u1", limit=200)
        assert len(mems) == 100  # 5 threads * 20 writes

    def test_concurrent_read_write(self, store):
        store.remember("tg", "u1", "initial memory for reads")
        errors = []

        def reader():
            try:
                for _ in range(20):
                    store.recall("tg", "u1", "initial")
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for i in range(20):
                    store.remember("tg", "u1", f"extra {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        threads += [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors

    def test_concurrent_forget(self, store):
        mems = [store.remember("tg", "u1", f"to delete {i}") for i in range(10)]
        errors = []

        def forgetter(mem):
            try:
                store.forget("tg", "u1", mem.id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=forgetter, args=(m,)) for m in mems]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert store.stats("tg", "u1")["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 8. Data integrity
# ══════════════════════════════════════════════════════════════════════════════


class TestDataIntegrity:
    def test_unique_ids(self, store):
        ids = set()
        for i in range(50):
            mem = store.remember("tg", "u1", f"unique {i}")
            assert mem.id not in ids
            ids.add(mem.id)

    def test_timestamps_increase(self, store):
        m1 = store.remember("tg", "u1", "first")
        time.sleep(0.01)
        m2 = store.remember("tg", "u1", "second")
        assert m2.created_at >= m1.created_at

    def test_update_preserves_unmodified_fields(self, store):
        mem = store.remember("tg", "u1", "original", tags=["keep"], importance=0.8)
        updated = store.update("tg", "u1", mem.id, content="new content")
        assert updated.tags == ["keep"]
        assert updated.importance == 0.8

    def test_forgotten_memory_not_in_recall(self, store):
        mem = store.remember("tg", "u1", "ephemeral thing")
        store.forget("tg", "u1", mem.id)
        results = store.recall("tg", "u1", "ephemeral")
        assert not any(r.id == mem.id for r in results)

    def test_forgotten_memory_not_in_list(self, store):
        mem = store.remember("tg", "u1", "gone")
        store.forget("tg", "u1", mem.id)
        mems = store.list_memories("tg", "u1")
        assert not any(m.id == mem.id for m in mems)

    def test_list_ordered_by_updated_at(self, store):
        store.remember("tg", "u1", "old")
        time.sleep(0.01)
        store.remember("tg", "u1", "new")
        mems = store.list_memories("tg", "u1")
        assert mems[0].content == "new"
        assert mems[1].content == "old"


# ══════════════════════════════════════════════════════════════════════════════
# 9. LIKE fallback when FTS unavailable
# ══════════════════════════════════════════════════════════════════════════════


class TestLikeFallback:
    def test_recall_without_fts_uses_like(self, store):
        store.remember("tg", "u1", "python is great")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "python")
        assert len(results) >= 1
        assert any("python" in r.content for r in results)

    def test_recall_without_fts_no_match(self, store):
        store.remember("tg", "u1", "java is okay")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "python")
        assert len(results) == 0

    def test_recall_without_fts_with_tags(self, store):
        store.remember("tg", "u1", "tagged content", tags=["lang"])
        store.remember("tg", "u1", "untagged content")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "content", tags=["lang"])
        assert len(results) == 1
        assert results[0].content == "tagged content"

    def test_has_fts_returns_false_when_fts_broken(self, store):
        mock_conn = unittest.mock.MagicMock()
        mock_conn.execute.side_effect = sqlite3.OperationalError("no such table")
        with unittest.mock.patch.object(store, "_conn", return_value=mock_conn):
            assert store._has_fts() is False

    def test_recall_without_fts_respects_limit(self, store):
        for i in range(10):
            store.remember("tg", "u1", f"item number {i}")
        with unittest.mock.patch.object(store, "_has_fts", return_value=False):
            results = store.recall("tg", "u1", "item", limit=3)
        assert len(results) <= 3


# ══════════════════════════════════════════════════════════════════════════════
# 10. get() by id  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetById:
    def test_get_returns_memory(self, store):
        mem = store.remember("tg", "u1", "fetch me", tags=["a"], importance=0.7)
        got = store.get("tg", "u1", mem.id)
        assert got is not None
        assert got.id == mem.id
        assert got.content == "fetch me"
        assert got.tags == ["a"]
        assert got.importance == 0.7

    def test_get_missing_returns_none(self, store):
        assert store.get("tg", "u1", "no-such-id") is None

    def test_get_wrong_platform_returns_none(self, store):
        mem = store.remember("tg", "u1", "x")
        assert store.get("slack", "u1", mem.id) is None

    def test_get_wrong_user_returns_none(self, store):
        mem = store.remember("tg", "u1", "x")
        assert store.get("tg", "u2", mem.id) is None

    def test_get_with_metadata(self, store):
        mem = store.remember("tg", "u1", "meta", metadata={"src": "chat"})
        got = store.get("tg", "u1", mem.id)
        assert got.metadata == {"src": "chat"}


# ══════════════════════════════════════════════════════════════════════════════
# 11. FTS index build / sync semantics  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestFTSIndexBuild:
    def test_fts_table_exists(self, store):
        # _has_fts probes the virtual table built in _init_schema.
        assert store._has_fts() is True

    def test_fts_reflects_updates(self, store):
        # The AFTER UPDATE trigger should re-index changed content.
        mem = store.remember("tg", "u1", "old keyword apple")
        store.update("tg", "u1", mem.id, content="new keyword banana")
        assert store.recall("tg", "u1", "banana")
        assert store.recall("tg", "u1", "apple") == []

    def test_fts_reflects_deletes(self, store):
        # The AFTER DELETE trigger removes rows from the index.
        mem = store.remember("tg", "u1", "deletable cherry")
        store.forget("tg", "u1", mem.id)
        assert store.recall("tg", "u1", "cherry") == []

    def test_has_fts_cached(self, store):
        # Second call uses the cached _fts_available attribute (no re-probe).
        assert store._has_fts() is True
        assert store._has_fts() is True
        assert store._fts_available is True

    def test_recall_fts_operational_error_falls_back_to_like(self, store):
        # When the FTS MATCH query itself raises OperationalError, recall
        # falls through to the LIKE branch (lines 273-274).
        store.remember("tg", "u1", "fallback target word")
        real_conn = store._conn()

        class FlakyConn:
            def execute(self, sql, *a, **k):
                if "memories_fts" in sql and "MATCH" in sql:
                    raise sqlite3.OperationalError("fts blew up")
                return real_conn.execute(sql, *a, **k)

        with unittest.mock.patch.object(store, "_conn", return_value=FlakyConn()):
            results = store.recall("tg", "u1", "target")
        assert any("target" in r.content for r in results)


# ══════════════════════════════════════════════════════════════════════════════
# 12. Schema upgrade — metadata column ALTER  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestSchemaUpgrade:
    def test_metadata_column_added_on_legacy_schema(self, tmp_path):
        # Simulate an old DB whose memories table predates the metadata column.
        db = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db)
        conn.executescript(
            """CREATE TABLE memories (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL,
                content TEXT NOT NULL, tags TEXT, importance REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL, updated_at REAL NOT NULL);"""
        )
        conn.commit()
        conn.close()

        # Constructing the store should ALTER in the metadata column.
        store = MemoryStore(db)
        cols = {r[1] for r in store._conn().execute("PRAGMA table_info(memories)")}
        assert "metadata" in cols
        # And it remains fully usable.
        mem = store.remember("tg", "u1", "post-upgrade", metadata={"k": "v"})
        assert store.get("tg", "u1", mem.id).metadata == {"k": "v"}


# ══════════════════════════════════════════════════════════════════════════════
# 13. Export / Import  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestExportImport:
    def test_export_all_shape(self, store):
        store.remember("tg", "u1", "first export", tags=["a"], importance=0.6,
                       metadata={"n": 1})
        store.remember("tg", "u1", "second export")
        exported = store.export_all("tg", "u1")
        assert len(exported) == 2
        first = exported[0]
        assert first["content"] == "first export"
        assert first["tags"] == ["a"]
        assert first["importance"] == 0.6
        assert first["metadata"] == {"n": 1}
        assert "created_at" in first

    def test_export_ordered_by_created_at_asc(self, store):
        store.remember("tg", "u1", "older")
        time.sleep(0.01)
        store.remember("tg", "u1", "newer")
        exported = store.export_all("tg", "u1")
        assert exported[0]["content"] == "older"
        assert exported[1]["content"] == "newer"

    def test_export_filters_by_tag(self, store):
        store.remember("tg", "u1", "work item", tags=["work"])
        store.remember("tg", "u1", "home item", tags=["home"])
        exported = store.export_all("tg", "u1", tags=["work"])
        assert len(exported) == 1
        assert exported[0]["content"] == "work item"

    def test_export_empty_user(self, store):
        assert store.export_all("tg", "nobody") == []

    def test_export_no_tags_no_metadata_defaults(self, store):
        store.remember("tg", "u1", "bare")
        exported = store.export_all("tg", "u1")
        assert exported[0]["tags"] == []
        assert exported[0]["metadata"] == {}

    def test_import_all_basic(self, store):
        entries = [
            {"content": "imported one", "tags": ["x"], "importance": 0.9},
            {"content": "imported two", "metadata": {"src": "file"}},
        ]
        result = store.import_all("tg", "u1", entries)
        assert result == {"imported": 2, "skipped": 0}
        mems = store.list_memories("tg", "u1")
        assert len(mems) == 2

    def test_import_skips_empty_content(self, store):
        entries = [
            {"content": "keep this"},
            {"content": "   "},
            {"content": ""},
            {},  # no content key at all
        ]
        result = store.import_all("tg", "u1", entries)
        assert result == {"imported": 1, "skipped": 3}

    def test_import_clamps_importance(self, store):
        result = store.import_all("tg", "u1", [{"content": "x", "importance": 5.0}])
        assert result["imported"] == 1
        mem = store.list_memories("tg", "u1")[0]
        assert mem.importance == 1.0

    def test_import_preserves_created_at(self, store):
        result = store.import_all(
            "tg", "u1", [{"content": "dated", "created_at": 12345.0}]
        )
        assert result["imported"] == 1
        exported = store.export_all("tg", "u1")
        assert exported[0]["created_at"] == 12345.0

    def test_export_then_import_roundtrip(self, store):
        store.remember("tg", "u1", "round trip", tags=["t"], importance=0.7,
                       metadata={"a": 1})
        dump = store.export_all("tg", "u1")
        store.import_all("tg", "u2", dump)
        restored = store.list_memories("tg", "u2")
        assert len(restored) == 1
        assert restored[0].content == "round trip"
        assert restored[0].tags == ["t"]
        assert restored[0].importance == 0.7
        assert restored[0].metadata == {"a": 1}


# ══════════════════════════════════════════════════════════════════════════════
# 14. AI-powered extraction  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractMemories:
    @pytest.mark.asyncio
    async def test_empty_text_returns_empty(self):
        from telechat_pkg.memory import extract_memories
        assert await extract_memories("   ") == []

    @pytest.mark.asyncio
    async def test_no_api_key_returns_raw_fallback(self, monkeypatch):
        from telechat_pkg import memory
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        out = await memory.extract_memories("remember I like tea")
        assert len(out) == 1
        assert out[0]["content"] == "remember I like tea"
        assert out[0]["tags"] == ["session"]
        assert out[0]["importance"] == 0.5

    @pytest.mark.asyncio
    async def test_long_text_truncated_in_fallback(self, monkeypatch):
        from telechat_pkg import memory
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        out = await memory.extract_memories("y" * 1000)
        assert len(out[0]["content"]) == 500

    @pytest.mark.asyncio
    async def test_successful_api_extraction(self, monkeypatch):
        from telechat_pkg import memory
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"content": [{"text": '[{"content":"likes go","tags":["lang"],"importance":0.8}]'}]}

        class FakeClient:
            async def post(self, *a, **k):
                return FakeResp()

        monkeypatch.setattr(memory, "_get_httpx_client", lambda: FakeClient())
        out = await memory.extract_memories("user said they like Go")
        assert out == [{"content": "likes go", "tags": ["lang"], "importance": 0.8}]

    @pytest.mark.asyncio
    async def test_api_error_falls_back_to_raw(self, monkeypatch):
        from telechat_pkg import memory
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        class FakeClient:
            async def post(self, *a, **k):
                raise RuntimeError("network down")

        monkeypatch.setattr(memory, "_get_httpx_client", lambda: FakeClient())
        out = await memory.extract_memories("conversation text here")
        assert len(out) == 1
        assert out[0]["content"] == "conversation text here"
        assert out[0]["tags"] == ["session"]

    def test_get_httpx_client_is_cached(self, monkeypatch):
        from telechat_pkg import memory
        memory._httpx_client = None
        sentinel = object()
        fake_httpx = unittest.mock.MagicMock()
        fake_httpx.AsyncClient.return_value = sentinel
        monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
        try:
            c1 = memory._get_httpx_client()
            c2 = memory._get_httpx_client()
            assert c1 is sentinel
            assert c1 is c2
            fake_httpx.AsyncClient.assert_called_once()
        finally:
            memory._httpx_client = None


# ══════════════════════════════════════════════════════════════════════════════
# 15. Default DB path (shares store.DB_PATH)  (ticket 0016)
# ══════════════════════════════════════════════════════════════════════════════


class TestDefaultDBPath:
    def test_none_db_path_uses_store(self, monkeypatch, tmp_path):
        from telechat_pkg import store
        p = str(tmp_path / "shared.db")
        monkeypatch.setattr(store, "DB_PATH", p)
        mem_store = MemoryStore()
        assert mem_store._db_path == p
        # Usable end to end against that path.
        m = mem_store.remember("tg", "u1", "shared path mem")
        assert mem_store.get("tg", "u1", m.id) is not None
