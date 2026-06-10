"""Persistent memory store backed by SQLite."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid


class MemoryStore:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                )
            """)

    def remember(self, content: str, tags: list[str]) -> dict:
        memory_id = uuid.uuid4().hex[:8]
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO memories (id, content, tags, created_at) VALUES (?, ?, ?, ?)",
                (memory_id, content, json.dumps(tags or []), time.time()),
            )
        return {"id": memory_id, "stored": True}

    def recall(self, query: str, tags: list[str], limit: int) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT 200"
            ).fetchall()

        results: list[dict] = []
        for row in rows:
            mem_tags: list[str] = json.loads(row["tags"])
            if tags and not any(t in mem_tags for t in tags):
                continue
            if query and query.lower() not in row["content"].lower():
                continue
            results.append({"id": row["id"], "content": row["content"], "tags": mem_tags})
            if len(results) >= limit:
                break
        return results

    def forget(self, memory_id: str) -> dict:
        with self._lock, self._conn() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            deleted = cursor.rowcount > 0
        return {"id": memory_id, "deleted": deleted}
