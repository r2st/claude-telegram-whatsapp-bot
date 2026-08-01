"""
Persistent memory layer for telechat.

Stores user memories in the same SQLite database (bot.db) with FTS5
full-text search and BM25 ranking weighted by importance scores.

Each memory belongs to a platform + user_id, so Telegram/WhatsApp/Slack
users each have their own memory namespace.

Usage:
    from telechat_pkg.memory import MemoryStore
    mem = MemoryStore()              # uses bot.db
    mem.remember("telegram", "123", "User prefers dark mode", tags=["preference"])
    results = mem.recall("telegram", "123", "dark mode")
    mem.forget("telegram", "123", "<uuid>")
    mem.export_all("telegram", "123")  # → list[dict] for JSON export
    mem.import_all("telegram", "123", [...])  # bulk import
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import models

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "You are a memory extraction system for an AI assistant. Given a conversation, "
    "extract the most important facts worth remembering for future sessions.\n\n"
    "Rules:\n"
    "- Extract 3–10 specific, concrete facts — never vague summaries\n"
    "- Each memory: 1–2 sentences, stands alone without conversation context\n"
    "- Prioritise: user preferences, technical decisions, project details, personal context\n"
    "- Skip: small talk, one-off tasks already done, anything obviously temporary\n"
    "- tags: 2–4 lowercase labels from: preference, project, tooling, deploy, infra, coding, "
    "editor, personal, workflow\n"
    "- importance: 0.9–1.0 critical preferences/key decisions | 0.6–0.8 useful context | 0.5 minor facts\n\n"
    'Return ONLY a valid JSON array, no prose:\n'
    '[{"content":"...","tags":["tag1","tag2"],"importance":0.8}]'
)


@dataclass
class Memory:
    id: str
    platform: str
    user_id: str
    content: str
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    created_at: float = 0.0
    updated_at: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResult(Memory):
    score: float = 0.0


class MemoryStore:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            # Share the canonical DB path with store.py so both modules agree
            # on the location (TELECHAT_HOME / ~/.telechat / bot.db), instead
            # of writing into the installed package directory.
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                content     TEXT NOT NULL,
                tags        TEXT,
                importance  REAL NOT NULL DEFAULT 0.5,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                metadata    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_user
                ON memories(platform, user_id);
            CREATE INDEX IF NOT EXISTS idx_memories_updated
                ON memories(updated_at DESC);
        """)

        # Add metadata column if upgrading from older schema
        try:
            conn.execute("SELECT metadata FROM memories LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE memories ADD COLUMN metadata TEXT")

        # FTS5 with Porter stemming for better recall
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    tags,
                    tokenize = 'porter unicode61',
                    content = 'memories',
                    content_rowid = 'rowid'
                )
            """)
        except sqlite3.OperationalError:  # pragma: no cover
            # FTS5 not available — fall back to LIKE search
            pass

        # Triggers to keep FTS in sync
        for trigger_sql in [
            """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, COALESCE(new.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.rowid, old.content, COALESCE(old.tags, ''));
            END""",
            """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                VALUES ('delete', old.rowid, old.content, COALESCE(old.tags, ''));
                INSERT INTO memories_fts(rowid, content, tags)
                VALUES (new.rowid, new.content, COALESCE(new.tags, ''));
            END""",
        ]:
            try:
                conn.execute(trigger_sql)
            except sqlite3.OperationalError:  # pragma: no cover
                pass

        conn.commit()

    def _has_fts(self) -> bool:
        if not hasattr(self, "_fts_available"):
            try:
                self._conn().execute("SELECT 1 FROM memories_fts LIMIT 0")
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False
        return self._fts_available

    _MEMORY_FIELDS = frozenset(Memory.__dataclass_fields__)

    def _parse_row(self, row: sqlite3.Row) -> Memory:
        meta_raw = row["metadata"] if "metadata" in row.keys() else None
        return Memory(
            id=row["id"],
            platform=row["platform"],
            user_id=row["user_id"],
            content=row["content"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            importance=row["importance"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(meta_raw) if meta_raw else {},
        )

    def _row_to_search_result(self, row: sqlite3.Row, score: float) -> SearchResult:
        d = {k: row[k] for k in self._MEMORY_FIELDS if k not in ("tags", "metadata")}
        d["tags"] = json.loads(row["tags"]) if row["tags"] else []
        meta_raw = row["metadata"] if "metadata" in row.keys() else None
        d["metadata"] = json.loads(meta_raw) if meta_raw else {}
        d["score"] = score
        return SearchResult(**d)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def remember(
        self,
        platform: str,
        user_id: str,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 0.5,
        metadata: dict | None = None,
    ) -> Memory:
        now = time.time()
        mem = Memory(
            id=str(uuid.uuid4()),
            platform=platform,
            user_id=user_id,
            content=content.strip(),
            tags=tags or [],
            importance=max(0.0, min(1.0, importance)),
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        self._conn().execute(
            """INSERT INTO memories (id, platform, user_id, content, tags, importance, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem.id, mem.platform, mem.user_id, mem.content,
             json.dumps(mem.tags) if mem.tags else None,
             mem.importance, mem.created_at, mem.updated_at,
             json.dumps(mem.metadata) if mem.metadata else None),
        )
        self._conn().commit()
        return mem

    def get(self, platform: str, user_id: str, memory_id: str) -> Memory | None:
        row = self._conn().execute(
            "SELECT * FROM memories WHERE id = ? AND platform = ? AND user_id = ?",
            (memory_id, platform, user_id),
        ).fetchone()
        return self._parse_row(row) if row else None

    def recall(
        self,
        platform: str,
        user_id: str,
        query: str,
        *,
        limit: int = 10,
        tags: list[str] | None = None,
        match_any: bool = False,
    ) -> list[SearchResult]:
        conn = self._conn()
        tag_clause = ""
        tag_params: list[str] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_clause = f"AND EXISTS (SELECT 1 FROM json_each(m.tags) WHERE value IN ({placeholders}))"
            tag_params = list(tags)

        query = query.strip()
        if not query:
            rows = conn.execute(
                f"""SELECT *, 0.0 as rank FROM memories m
                    WHERE platform = ? AND user_id = ?
                    {tag_clause}
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?""",
                [platform, user_id, *tag_params, limit],
            ).fetchall()
            return [self._row_to_search_result(r, 0.0) for r in rows]

        # Try FTS5 search first
        if self._has_fts():
            fts_query = self._to_fts_query(query, match_any)
            try:
                rows = conn.execute(
                    f"""SELECT m.*, f.rank
                        FROM memories_fts f
                        JOIN memories m ON m.rowid = f.rowid
                        WHERE memories_fts MATCH ?
                          AND m.platform = ? AND m.user_id = ?
                          {tag_clause}
                        ORDER BY f.rank * (1.0 / m.importance)
                        LIMIT ?""",
                    [fts_query, platform, user_id, *tag_params, limit],
                ).fetchall()
                return [self._row_to_search_result(r, r["rank"]) for r in rows]
            except sqlite3.OperationalError:
                pass

        # Fallback: LIKE search
        rows = conn.execute(
            f"""SELECT *, 0.0 as rank FROM memories m
                WHERE platform = ? AND user_id = ?
                  AND content LIKE '%' || ? || '%'
                  {tag_clause}
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?""",
            [platform, user_id, query, *tag_params, limit],
        ).fetchall()
        return [self._row_to_search_result(r, 0.0) for r in rows]

    def forget(self, platform: str, user_id: str, memory_id: str) -> bool:
        result = self._conn().execute(
            "DELETE FROM memories WHERE id = ? AND platform = ? AND user_id = ?",
            (memory_id, platform, user_id),
        )
        self._conn().commit()
        return result.rowcount > 0

    def update(
        self,
        platform: str,
        user_id: str,
        memory_id: str,
        *,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        metadata: dict | None = None,
    ) -> Memory | None:
        existing = self._conn().execute(
            "SELECT * FROM memories WHERE id = ? AND platform = ? AND user_id = ?",
            (memory_id, platform, user_id),
        ).fetchone()
        if not existing:
            return None

        new_content = content if content is not None else existing["content"]
        new_tags = tags if tags is not None else (json.loads(existing["tags"]) if existing["tags"] else [])
        new_importance = max(0.0, min(1.0, importance)) if importance is not None else existing["importance"]
        old_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        new_metadata = metadata if metadata is not None else old_meta

        self._conn().execute(
            """UPDATE memories SET content = ?, tags = ?, importance = ?, metadata = ?, updated_at = ?
               WHERE id = ?""",
            (new_content,
             json.dumps(new_tags) if new_tags else None,
             new_importance,
             json.dumps(new_metadata) if new_metadata else None,
             time.time(),
             memory_id),
        )
        self._conn().commit()
        return self._parse_row(self._conn().execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone())

    def list_memories(
        self,
        platform: str,
        user_id: str,
        *,
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> list[Memory]:
        tag_clause = ""
        tag_params: list[str] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_clause = f"AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN ({placeholders}))"
            tag_params = list(tags)

        rows = self._conn().execute(
            f"""SELECT * FROM memories
                WHERE platform = ? AND user_id = ?
                {tag_clause}
                ORDER BY updated_at DESC
                LIMIT ?""",
            [platform, user_id, *tag_params, limit],
        ).fetchall()
        return [self._parse_row(r) for r in rows]

    def stats(self, platform: str, user_id: str) -> dict:
        row = self._conn().execute(
            """SELECT COUNT(*) as total,
                      MIN(created_at) as oldest,
                      MAX(created_at) as newest
               FROM memories WHERE platform = ? AND user_id = ?""",
            (platform, user_id),
        ).fetchone()
        return {"total": row["total"], "oldest": row["oldest"], "newest": row["newest"]}

    # ── Export / Import ───────────────────────────────────────────────────

    def export_all(
        self,
        platform: str,
        user_id: str,
        *,
        tags: list[str] | None = None,
    ) -> list[dict]:
        tag_clause = ""
        tag_params: list[str] = []
        if tags:
            placeholders = ",".join("?" for _ in tags)
            tag_clause = f"AND EXISTS (SELECT 1 FROM json_each(tags) WHERE value IN ({placeholders}))"
            tag_params = list(tags)

        rows = self._conn().execute(
            f"""SELECT * FROM memories
                WHERE platform = ? AND user_id = ?
                {tag_clause}
                ORDER BY created_at ASC""",
            [platform, user_id, *tag_params],
        ).fetchall()
        return [
            {
                "content": r["content"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "importance": r["importance"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def import_all(
        self,
        platform: str,
        user_id: str,
        entries: list[dict],
    ) -> dict:
        imported = 0
        skipped = 0
        conn = self._conn()
        for entry in entries:
            content = (entry.get("content") or "").strip()
            if not content:
                skipped += 1
                continue
            now = time.time()
            tags = entry.get("tags") or []
            importance = max(0.0, min(1.0, entry.get("importance", 0.5)))
            created_at = entry.get("created_at", now)
            metadata = entry.get("metadata") or {}
            conn.execute(
                """INSERT INTO memories (id, platform, user_id, content, tags, importance, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), platform, user_id, content,
                 json.dumps(tags) if tags else None,
                 importance, created_at, now,
                 json.dumps(metadata) if metadata else None),
            )
            imported += 1
        conn.commit()
        return {"imported": imported, "skipped": skipped}

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_fts_query(raw: str, match_any: bool = False) -> str:
        """Quote each token so user punctuation cannot become FTS5 syntax.

        Tokens are joined with FTS5's implicit AND, which is right for `/recall`
        where someone types the two or three words they remember. It is wrong
        when the query is a whole chat message — requiring every word of "how do
        I deploy this?" to appear in one memory matches nothing, ever. Callers
        searching with a sentence pass ``match_any``.
        """
        tokens = raw.strip().split()
        if not tokens:
            return '""'
        quoted = [f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in tokens]
        return (" OR " if match_any else " ").join(quoted)


# ── AI-powered memory extraction ──────────────────────────────────────────

_httpx_client = None


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None:
        import httpx
        _httpx_client = httpx.AsyncClient(timeout=20)
    return _httpx_client


async def extract_memories(text: str) -> list[dict]:
    """Extract structured memories from conversation text using Claude API.
    Falls back to storing raw text as a single memory when no API key is set."""
    trimmed = text.strip()
    if not trimmed:
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return [{"content": trimmed[:500], "tags": ["session"], "importance": 0.5}]

    try:
        client = _get_httpx_client()
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": models.HAIKU,
                "max_tokens": 1024,
                "system": EXTRACTION_PROMPT,
                "messages": [{"role": "user", "content": trimmed}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["content"][0]["text"])
    except Exception as e:
        log.warning("Memory extraction failed, storing raw: %s", e)
        return [{"content": trimmed[:500], "tags": ["session"], "importance": 0.5}]
