"""
Knowledge Base / RAG (Feature 9) — document ingestion and retrieval-augmented generation.

Inspired by the Customer Support Agent quickstart (Amazon Bedrock Knowledge Bases)
and Wisedocs' document verification pipeline. Provides local document ingestion
with chunking and FTS search for context injection into Claude prompts.

Supports: .txt, .md, .pdf (text extract), .json, .csv, .py, .js, .ts, .html

Usage:
    from telechat_pkg.knowledge_base import KnowledgeBase
    kb = KnowledgeBase()
    kb.ingest_text("telegram", "123", "API Docs", content, tags=["docs"])
    kb.ingest_file("telegram", "123", "/path/to/doc.md")
    results = kb.search("telegram", "123", "how to authenticate")
    context = kb.build_context("telegram", "123", "user query here")
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KB_ENABLED = os.getenv("KB_ENABLED", "true").lower() in ("1", "true", "yes")
KB_CHUNK_SIZE = int(os.getenv("KB_CHUNK_SIZE", "1000"))  # chars per chunk
KB_CHUNK_OVERLAP = int(os.getenv("KB_CHUNK_OVERLAP", "200"))
KB_MAX_CONTEXT_CHUNKS = int(os.getenv("KB_MAX_CONTEXT_CHUNKS", "5"))
KB_MAX_CONTEXT_CHARS = int(os.getenv("KB_MAX_CONTEXT_CHARS", "4000"))


def _to_fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Each whitespace-separated token becomes a quoted phrase, and any embedded
    double quote is doubled — the FTS5 escape. Without the doubling a search
    containing a quote (`say "hi"`) produces malformed syntax, MATCH raises,
    and the caller silently falls back to a full LIKE scan: the index quietly
    stops being used for exactly the queries a user typed carefully.

    memory.py has always escaped this way; the knowledge base did not.
    """
    tokens = raw.strip().split()
    if not tokens:
        return '""'
    return " ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)


@dataclass
class Document:
    id: str
    platform: str
    user_id: str
    title: str
    source: str  # file path or URL or "upload"
    content_hash: str
    chunk_count: int
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class Chunk:
    id: str
    doc_id: str
    content: str
    chunk_index: int
    score: float = 0.0


@dataclass
class SearchResult:
    chunk: Chunk
    document: Document
    score: float


class KnowledgeBase:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            # Share the canonical DB location with store.py so KB docs and
            # chunks live in the same SQLite file as the rest of the bot,
            # not in the installed package directory.
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

    def _init_schema(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kb_documents (
                id          TEXT PRIMARY KEY,
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                title       TEXT NOT NULL,
                source      TEXT,
                content_hash TEXT,
                chunk_count INTEGER DEFAULT 0,
                tags        TEXT,
                created_at  REAL NOT NULL,
                metadata    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_kb_docs_user
                ON kb_documents(platform, user_id);

            CREATE TABLE IF NOT EXISTS kb_chunks (
                id          TEXT PRIMARY KEY,
                doc_id      TEXT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                content     TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                platform    TEXT NOT NULL,
                user_id     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc
                ON kb_chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_kb_chunks_user
                ON kb_chunks(platform, user_id);
        """)

        # FTS5 for chunk search
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                    content,
                    tokenize = 'porter unicode61',
                    content = 'kb_chunks',
                    content_rowid = 'rowid'
                )
            """)
        except sqlite3.OperationalError:  # pragma: no cover
            pass

        # Sync triggers
        for sql in [
            """CREATE TRIGGER IF NOT EXISTS kb_chunks_ai AFTER INSERT ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(rowid, content) VALUES (new.rowid, new.content);
            END""",
            """CREATE TRIGGER IF NOT EXISTS kb_chunks_ad AFTER DELETE ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
            END""",
        ]:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:  # pragma: no cover
                pass

        conn.commit()

    def _has_fts(self) -> bool:
        if not hasattr(self, "_fts_ok"):
            try:
                self._conn().execute("SELECT 1 FROM kb_chunks_fts LIMIT 0")
                self._fts_ok = True
            except sqlite3.OperationalError:
                self._fts_ok = False
        return self._fts_ok

    # ── Chunking ──────────────────────────────────────────────────────────

    @staticmethod
    def chunk_text(text: str, chunk_size: int = KB_CHUNK_SIZE, overlap: int = KB_CHUNK_OVERLAP) -> list[str]:
        """Split text into overlapping chunks, breaking at sentence boundaries."""
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            if end < len(text):
                # Find a good break point (sentence end or paragraph)
                for sep in ["\n\n", "\n", ". ", "! ", "? ", "; ", ", "]:
                    break_at = text.rfind(sep, start + chunk_size // 2, end)
                    if break_at > start:
                        end = break_at + len(sep)
                        break
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - overlap if end < len(text) else len(text)

        return chunks

    # ── Ingestion ─────────────────────────────────────────────────────────

    def ingest_text(
        self,
        platform: str,
        user_id: str,
        title: str,
        content: str,
        *,
        source: str = "upload",
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> Document:
        """Ingest text content into the knowledge base."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # Check for duplicate
        existing = self._conn().execute(
            "SELECT id FROM kb_documents WHERE platform = ? AND user_id = ? AND content_hash = ?",
            (platform, user_id, content_hash),
        ).fetchone()
        if existing:
            log.info("Document already ingested (hash=%s)", content_hash)
            return self._get_document(existing["id"])

        doc_id = str(uuid.uuid4())
        chunks = self.chunk_text(content)

        conn = self._conn()
        conn.execute(
            """INSERT INTO kb_documents (id, platform, user_id, title, source, content_hash, chunk_count, tags, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, platform, user_id, title, source, content_hash, len(chunks),
             json.dumps(tags) if tags else None, time.time(),
             json.dumps(metadata) if metadata else None),
        )

        for i, chunk in enumerate(chunks):
            chunk_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO kb_chunks (id, doc_id, content, chunk_index, platform, user_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chunk_id, doc_id, chunk, i, platform, user_id),
            )

        conn.commit()
        log.info("Ingested document '%s' (%d chunks) for %s/%s", title, len(chunks), platform, user_id)
        return self._get_document(doc_id)

    def ingest_file(
        self,
        platform: str,
        user_id: str,
        file_path: str,
        *,
        tags: list[str] | None = None,
    ) -> Document | None:
        """Ingest a file into the knowledge base."""
        path = Path(file_path)
        if not path.exists():
            log.error("File not found: %s", file_path)
            return None

        # Read content based on extension
        ext = path.suffix.lower()
        try:
            if ext in (".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".json", ".csv", ".yaml", ".yml", ".toml"):
                content = path.read_text(errors="replace")
            elif ext == ".pdf":
                content = self._extract_pdf(path)
            else:
                log.warning("Unsupported file type: %s", ext)
                return None
        except Exception as e:
            log.error("Failed to read file %s: %s", file_path, e)
            return None

        if not content.strip():
            return None

        return self.ingest_text(
            platform, user_id,
            title=path.name,
            content=content,
            source=str(path),
            tags=tags or [ext.lstrip(".")],
        )

    @staticmethod
    def _extract_pdf(path: Path) -> str:
        """Extract text from PDF using pypdf if available."""
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            log.warning("pypdf not installed, cannot extract PDF text")
            return ""

    # ── Search ────────────────────────────────────────────────────────────

    def search(
        self,
        platform: str,
        user_id: str,
        query: str,
        *,
        limit: int = KB_MAX_CONTEXT_CHUNKS,
        tags: list[str] | None = None,
    ) -> list[SearchResult]:
        """Search the knowledge base using FTS."""
        conn = self._conn()

        if self._has_fts() and query.strip():
            fts_query = _to_fts_query(query)
            try:
                rows = conn.execute(
                    """SELECT c.*, f.rank, d.title, d.tags as doc_tags, d.source, d.created_at as doc_created
                       FROM kb_chunks_fts f
                       JOIN kb_chunks c ON c.rowid = f.rowid
                       JOIN kb_documents d ON d.id = c.doc_id
                       WHERE kb_chunks_fts MATCH ?
                         AND c.platform = ? AND c.user_id = ?
                       ORDER BY f.rank
                       LIMIT ?""",
                    (fts_query, platform, user_id, limit),
                ).fetchall()
                return [
                    SearchResult(
                        chunk=Chunk(id=r["id"], doc_id=r["doc_id"], content=r["content"],
                                    chunk_index=r["chunk_index"], score=r["rank"]),
                        document=self._get_document(r["doc_id"]),
                        score=r["rank"],
                    )
                    for r in rows
                ]
            except sqlite3.OperationalError:
                pass

        # Fallback: LIKE search
        rows = conn.execute(
            """SELECT c.*, d.title, d.tags as doc_tags
               FROM kb_chunks c
               JOIN kb_documents d ON d.id = c.doc_id
               WHERE c.platform = ? AND c.user_id = ?
                 AND c.content LIKE '%' || ? || '%'
               LIMIT ?""",
            (platform, user_id, query, limit),
        ).fetchall()
        return [
            SearchResult(
                chunk=Chunk(id=r["id"], doc_id=r["doc_id"], content=r["content"],
                            chunk_index=r["chunk_index"]),
                document=self._get_document(r["doc_id"]),
                score=0.0,
            )
            for r in rows
        ]

    def build_context(self, platform: str, user_id: str, query: str) -> str:
        """Build RAG context string for a user query."""
        if not KB_ENABLED:
            return ""
        results = self.search(platform, user_id, query)
        if not results:
            return ""

        context_parts = []
        total_chars = 0
        for r in results:
            chunk_text = f"[From: {r.document.title}]\n{r.chunk.content}"
            if total_chars + len(chunk_text) > KB_MAX_CONTEXT_CHARS:
                break
            context_parts.append(chunk_text)
            total_chars += len(chunk_text)

        if not context_parts:
            return ""

        return "\n\n---\n[Knowledge Base Context]\n" + "\n\n".join(context_parts)

    # ── Document management ───────────────────────────────────────────────

    def list_documents(self, platform: str, user_id: str, limit: int = 20) -> list[Document]:
        rows = self._conn().execute(
            """SELECT * FROM kb_documents
               WHERE platform = ? AND user_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (platform, user_id, limit),
        ).fetchall()
        return [self._parse_doc(r) for r in rows]

    def delete_document(self, platform: str, user_id: str, doc_id: str) -> bool:
        conn = self._conn()
        conn.execute("DELETE FROM kb_chunks WHERE doc_id = ? AND platform = ? AND user_id = ?",
                     (doc_id, platform, user_id))
        result = conn.execute("DELETE FROM kb_documents WHERE id = ? AND platform = ? AND user_id = ?",
                              (doc_id, platform, user_id))
        conn.commit()
        return result.rowcount > 0

    def stats(self, platform: str, user_id: str) -> dict:
        conn = self._conn()
        docs = conn.execute(
            "SELECT COUNT(*) as cnt FROM kb_documents WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) as cnt FROM kb_chunks WHERE platform = ? AND user_id = ?",
            (platform, user_id),
        ).fetchone()
        return {"documents": docs["cnt"], "chunks": chunks["cnt"]}

    def _get_document(self, doc_id: str) -> Document:
        row = self._conn().execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
        return self._parse_doc(row)

    def _parse_doc(self, row) -> Document:
        return Document(
            id=row["id"],
            platform=row["platform"],
            user_id=row["user_id"],
            title=row["title"],
            source=row["source"] or "",
            content_hash=row["content_hash"] or "",
            chunk_count=row["chunk_count"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )
