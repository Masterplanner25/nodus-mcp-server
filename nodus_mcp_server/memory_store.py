"""SQLite-backed persistent memory store."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid


class MemoryStore:
    def __init__(self, db_path: str) -> None:
        import os
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)

    def remember(self, content: str, tags: list[str]) -> dict:
        mem_id = str(uuid.uuid4())[:8]
        tags_str = ",".join(t.strip().lower() for t in tags if t.strip())
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO memories (id, content, tags, created_at) VALUES (?, ?, ?, ?)",
                (mem_id, content, tags_str, time.time()),
            )
        return {"id": mem_id, "stored": True}

    def recall(self, query: str, tags: list[str], limit: int) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT 200"
            ).fetchall()

        results = []
        query_lower = query.lower()
        filter_tags = {t.strip().lower() for t in tags if t.strip()}

        for row in rows:
            content = row["content"]
            row_tags = {t.lower() for t in row["tags"].split(",") if t}
            if query_lower and query_lower not in content.lower():
                continue
            if filter_tags and not filter_tags.intersection(row_tags):
                continue
            results.append({
                "id": row["id"],
                "content": content,
                "tags": list(row_tags),
                "created_at": row["created_at"],
            })
            if len(results) >= limit:
                break

        return results

    def forget(self, memory_id: str) -> dict:
        with self._lock, self._conn() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return {"id": memory_id, "deleted": cursor.rowcount > 0}
