"""Tests for MemoryStore — SQLite-backed persistent memory."""
import os
import tempfile
import pytest
from nodus_mcp_server.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(str(tmp_path / "test.db"))


def test_remember_returns_id(store):
    result = store.remember("hello world", [])
    assert result["stored"] is True
    assert isinstance(result["id"], str)
    assert len(result["id"]) > 0


def test_recall_by_content(store):
    store.remember("the quick brown fox", [])
    store.remember("unrelated entry", [])
    results = store.recall("quick brown", [], 10)
    assert len(results) == 1
    assert "quick brown fox" in results[0]["content"]


def test_recall_by_tag(store):
    store.remember("tagged entry", ["important", "work"])
    store.remember("other entry", ["personal"])
    results = store.recall("", ["important"], 10)
    assert len(results) == 1
    assert results[0]["content"] == "tagged entry"


def test_recall_empty_query_returns_all(store):
    store.remember("first", [])
    store.remember("second", [])
    results = store.recall("", [], 10)
    assert len(results) == 2


def test_recall_respects_limit(store):
    for i in range(5):
        store.remember(f"entry {i}", [])
    results = store.recall("", [], 3)
    assert len(results) == 3


def test_forget_removes_entry(store):
    r = store.remember("to be deleted", [])
    mem_id = r["id"]
    result = store.forget(mem_id)
    assert result["deleted"] is True
    remaining = store.recall("to be deleted", [], 10)
    assert len(remaining) == 0


def test_forget_missing_id(store):
    result = store.forget("nonexistent")
    assert result["deleted"] is False


def test_recall_tag_comma_string_format(store):
    store.remember("content", ["alpha", "beta"])
    results = store.recall("", ["alpha"], 10)
    assert len(results) == 1
    assert "alpha" in results[0]["tags"]


def test_multiple_stores_same_db(tmp_path):
    db = str(tmp_path / "shared.db")
    s1 = MemoryStore(db)
    s2 = MemoryStore(db)
    s1.remember("from s1", [])
    results = s2.recall("from s1", [], 10)
    assert len(results) == 1
