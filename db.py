"""SQLite persistence for sources, intelligence items, and conversational memory."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return (url or "").strip()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def normalize_title(title: str) -> str:
    return re.sub(r"\W+", " ", (title or "").lower(), flags=re.UNICODE).strip()


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.getenv("DATABASE_PATH", "./data/intel_radar.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    published_at TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    ai_summary TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_articles_created ON articles(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_articles_notified ON articles(notified, created_at);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id DESC);

                CREATE TABLE IF NOT EXISTS bot_chats (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feed_state (
                    source_url TEXT PRIMARY KEY,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_type TEXT NOT NULL CHECK(source_type IN ('rss', 'telegram', 'website', 'x', 'linkedin')),
                    locator TEXT NOT NULL UNIQUE,
                    categories TEXT NOT NULL DEFAULT 'general',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled, source_type);

                CREATE TABLE IF NOT EXISTS article_sources (
                    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    external_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(article_id, source_id)
                );
                """
            )
            self._add_column(conn, "articles", "canonical_url", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "articles", "external_id", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "articles", "title_hash", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "articles", "relevance_score", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "articles", "relevance_level", "TEXT NOT NULL DEFAULT 'low'")
            self._add_column(conn, "articles", "relevance_reason", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "articles", "entities", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_fingerprint ON articles(title_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_relevance ON articles(relevance_level, notified)")
            conn.execute(
                """UPDATE articles SET canonical_url=CASE WHEN canonical_url='' THEN url ELSE canonical_url END,
                   title_hash=CASE WHEN title_hash='' THEN ? ELSE title_hash END
                   WHERE canonical_url='' OR title_hash=''""",
                (title_hash(""),),
            )
            conn.execute(
                "UPDATE articles SET title_hash=lower(hex(randomblob(16))) WHERE title_hash=?",
                (title_hash(""),),
            )

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def seed_sources(self, feeds: Iterable[dict[str, Any]]) -> int:
        added = 0
        for feed in feeds:
            if not feed.get("name") or not feed.get("url"):
                continue
            if self.add_source(
                name=str(feed["name"]),
                source_type="rss",
                locator=str(feed["url"]),
                categories=str(feed.get("category", "general")),
                priority=int(feed.get("priority", 3)),
            ):
                added += 1
        return added

    def add_source(
        self, *, name: str, source_type: str, locator: str, categories: str = "general", priority: int = 3
    ) -> int | None:
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO sources
                       (name, source_type, locator, categories, priority, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name.strip()[:200], source_type, locator.strip()[:500], categories.strip()[:300],
                     priority, utc_now(), utc_now()),
                )
            except sqlite3.IntegrityError:
                return None
            return int(cur.lastrowid)

    def remove_source(self, name_or_locator: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sources WHERE name=? OR locator=?", (name_or_locator, name_or_locator))
            return cur.rowcount > 0

    def set_source_enabled(self, name_or_locator: str, enabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sources SET enabled=?, updated_at=? WHERE name=? OR locator=?",
                (1 if enabled else 0, utc_now(), name_or_locator, name_or_locator),
            )
            return cur.rowcount > 0

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY priority DESC, name COLLATE NOCASE"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql)]

    def source_by_id(self, source_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def update_source_state(self, source_id: int, *, error: str = "", checked: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE sources SET last_checked_at=CASE WHEN ? THEN ? ELSE last_checked_at END,
                   last_error=?, updated_at=? WHERE id=?""",
                (1 if checked else 0, utc_now(), error[:1000], utc_now(), source_id),
            )

    def register_chat(self, chat_id: int, user_id: int, username: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO bot_chats(chat_id, user_id, username, alerts_enabled, created_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,
                   username=excluded.username""",
                (chat_id, user_id, username or "", utc_now()),
            )

    def list_active_chats(self) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT chat_id FROM bot_chats WHERE alerts_enabled=1").fetchall()
        return [int(r["chat_id"]) for r in rows]

    def set_alerts(self, chat_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE bot_chats SET alerts_enabled=? WHERE chat_id=?", (1 if enabled else 0, chat_id))

    def insert_article(
        self, *, url: str, title: str, source: str, category: str, published_at: str,
        content: str, baseline: bool, source_id: int | None = None, external_id: str = "",
        relevance_score: int = 0, relevance_level: str = "low", relevance_reason: str = "",
        entities: str = "",
    ) -> int | None:
        canonical = normalize_url(url)
        fingerprint = title_hash(title)
        with self._connect() as conn:
            existing = conn.execute(
                """SELECT id FROM articles WHERE url=? OR canonical_url=? OR title_hash=?
                   OR (? <> '' AND external_id=? AND source=?) LIMIT 1""",
                (url, canonical, fingerprint, external_id, external_id, source),
            ).fetchone()
            if existing:
                article_id = int(existing["id"])
                if source_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO article_sources(article_id, source_id, external_id) VALUES (?, ?, ?)",
                        (article_id, source_id, external_id),
                    )
                return None
            cur = conn.execute(
                """INSERT INTO articles
                   (url, canonical_url, external_id, title_hash, title, source, category, published_at,
                    content, ai_summary, created_at, notified, relevance_score, relevance_level,
                    relevance_reason, entities)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)""",
                (url, canonical, external_id[:300], fingerprint, title[:600], source[:200], category[:80],
                 published_at, content[:12000], utc_now(), 1 if baseline else 0, relevance_score,
                 relevance_level, relevance_reason[:1000], entities[:1000]),
            )
            article_id = int(cur.lastrowid)
            if source_id:
                conn.execute(
                    "INSERT OR IGNORE INTO article_sources(article_id, source_id, external_id) VALUES (?, ?, ?)",
                    (article_id, source_id, external_id),
                )
            return article_id

    def update_article_summary(self, article_id: int, summary: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE articles SET ai_summary=? WHERE id=?", (summary[:5000], article_id))

    def pending_articles(self, limit: int = 4, minimum_score: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM articles WHERE notified=0 AND relevance_score>=?
                   ORDER BY relevance_score DESC, created_at ASC LIMIT ?""",
                (minimum_score, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, article_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE articles SET notified=1 WHERE id=?", (article_id,))

    def recent_articles(self, limit: int = 10, hours: int | None = None, minimum_score: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM articles"
        args: list[Any] = []
        clauses: list[str] = []
        if hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
            clauses.append("created_at >= ?")
            args.append(cutoff)
        if minimum_score is not None:
            clauses.append("relevance_score >= ?")
            args.append(minimum_score)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY relevance_score DESC, created_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def search_articles(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        terms = _keywords(query)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM articles ORDER BY relevance_score DESC, created_at DESC LIMIT 800").fetchall()
        articles = [dict(r) for r in rows]
        if not terms:
            return articles[:limit]
        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for article in articles:
            title = (article.get("title") or "").lower()
            body = " ".join(str(article.get(k) or "") for k in ("source", "category", "content", "ai_summary", "entities")).lower()
            score = sum(4 if term in title else 0 for term in terms) + sum(1 for term in terms if term in body)
            if score:
                ranked.append((score + int(article.get("relevance_score") or 0) // 10, str(article.get("created_at", "")), article))
        if not ranked:
            return articles[:limit]
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [x[2] for x in ranked[:limit]]

    def store_message(self, chat_id: int, role: str, content: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("INSERT INTO messages(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                               (chat_id, role, content[:12000], utc_now()))
            return int(cur.lastrowid)

    def recent_messages(self, chat_id: int, limit: int = 16) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def search_older_memories(self, chat_id: int, query: str, skip_latest: int = 16, limit: int = 4) -> list[dict[str, Any]]:
        terms = _keywords(query)
        if not terms:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 600", (chat_id,)).fetchall()
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for row in list(rows)[skip_latest:]:
            item = dict(row)
            score = sum(item["content"].lower().count(term) for term in terms)
            if score:
                ranked.append((score, int(item["id"]), item))
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [x[2] for x in ranked[:limit]]

    def forget_messages(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))

    def feed_is_initialized(self, source_url: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT initialized FROM feed_state WHERE source_url=?", (source_url,)).fetchone()
        return bool(row and row["initialized"])

    def mark_feed_initialized(self, source_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO feed_state(source_url, initialized, last_checked_at) VALUES (?, 1, ?)
                   ON CONFLICT(source_url) DO UPDATE SET initialized=1, last_checked_at=excluded.last_checked_at""",
                (source_url, utc_now()),
            )

    def update_feed_checked(self, source_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO feed_state(source_url, initialized, last_checked_at) VALUES (?, 0, ?)
                   ON CONFLICT(source_url) DO UPDATE SET last_checked_at=excluded.last_checked_at""",
                (source_url, utc_now()),
            )


def _keywords(text: str) -> list[str]:
    stopwords = {
        "the", "and", "for", "that", "this", "with", "from", "what", "when", "where", "which",
        "have", "about", "were", "was", "are", "you", "your", "can", "could", "would", "should",
        "tell", "give", "show", "does", "did", "into", "then", "than", "them", "they", "our",
        "their", "talked", "talk", "latest", "news", "update", "updates", "more", "there", "here",
        "just", "also", "today", "appeared", "important",
    }
    words = re.findall(r"[a-zA-Z0-9_.+-]{3,}", text.lower())
    return list(dict.fromkeys(w for w in words if w not in stopwords))[:12]
