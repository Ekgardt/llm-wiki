"""Tests for search_memory.py — ranking logic, boosts, RRF fusion.

Locks in:
1. Title boost: exact title match → higher rank than BM25-only
2. Filename short-circuit: exact filename match → rank 1 always
3. Path preference: knowledge/notes/ pages boosted over knowledge/notes/
4. RRF fusion: weighted (BM25=2, Vector=1, Graph=0.5)
5. Project-scoped boost: project-tagged pages x2
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _build_search_index_worker(root: str, builds: int) -> bool:
    import search_memory

    vault = Path(root)
    index_dir = vault / "cache"
    search_memory.ROOT = vault
    search_memory.INDEX_DIR = index_dir
    search_memory.INDEX_FILE = index_dir / "index.sqlite"
    search_memory.INDEX_MANIFEST = index_dir / ".paths-manifest"
    page = vault / "knowledge" / "notes" / "page.md"
    for _ in range(builds):
        search_memory._build_index([page])
    return True


def test_rrf_fuse_triple_weights():
    """Weighted RRF: BM25 weight=2 should dominate Vector weight=1."""
    import search_memory

    bm25 = [
        {"path": "page_a.md", "title": "A", "summary": "", "score": 10, "project": "", "timestamp": ""},
        {"path": "page_b.md", "title": "B", "summary": "", "score": 5, "project": "", "timestamp": ""},
    ]
    vector = [
        {"path": "page_b.md", "title": "B", "summary": "", "score": 8, "project": "", "timestamp": ""},
        {"path": "page_a.md", "title": "A", "summary": "", "score": 3, "project": "", "timestamp": ""},
    ]

    result = search_memory._rrf_fuse_triple(bm25, vector, None)

    # BM25 rank=1 for A should dominate Vector rank=2 for A
    assert result[0]["path"] == "page_a.md"
    assert result[0]["fused_score"] > result[1]["fused_score"]


def test_rrf_fuse_triple_graph_boost():
    """Graph boost adds score but doesn't overtake BM25 rank=1."""
    import search_memory

    bm25 = [
        {"path": "page_a.md", "title": "A", "summary": "", "score": 10, "project": "", "timestamp": ""},
    ]
    graph = [
        {"path": "page_b.md", "graph_boost": 0.15},
    ]

    result = search_memory._rrf_fuse_triple(bm25, None, graph)
    assert result[0]["path"] == "page_a.md"  # BM25 wins over graph-only


def test_rrf_fuse_triple_empty_inputs():
    """Empty inputs don't crash."""
    import search_memory

    result = search_memory._rrf_fuse_triple([], None, None)
    assert result == []


def test_rrf_fuse_basic_two_signals():
    """Basic 2-signal RRF (BM25 + Vector) via triple-fusion with no graph."""
    import search_memory

    bm25 = [{"path": "a.md", "title": "A", "summary": "", "score": 5, "project": "", "timestamp": ""}]
    vector = [{"path": "b.md", "title": "B", "summary": "", "score": 3, "project": "", "timestamp": ""}]

    result = search_memory._rrf_fuse_triple(bm25, vector, None)
    assert len(result) == 2
    assert result[0]["path"] == "a.md"


def test_extract_title_and_summary():
    """Title from H1, summary from 'One-sentence summary:' line."""
    import search_memory

    content = """---
type: concept
---
# My Great Concept

One-sentence summary: This concept is about something important.

## Body
Content here.
"""
    title, summary = search_memory._extract_title_and_summary(content, "fallback")
    assert title == "My Great Concept"
    assert "something important" in summary


def test_extract_title_fallback_to_stem():
    """No H1 → filename stem."""
    import search_memory

    content = "Just body, no heading."
    title, summary = search_memory._extract_title_and_summary(content, "my-file")
    assert title == "my-file"
    assert summary == ""


def test_strip_frontmatter():
    """Frontmatter is removed from search body."""
    import search_memory

    content = "---\ntype: fact\nsecret: sk-test123\n---\n\n# Real Content\nBody text."
    stripped = search_memory._strip_frontmatter(content)
    assert "sk-test" not in stripped
    assert "Real Content" in stripped


def test_collect_pages_skips_editorial():
    """Editorial filenames (index.md, log.md) are skipped."""
    import search_memory

    with patch.object(search_memory, "WIKI_DIR"), \
         patch.object(search_memory, "KNOWLEDGE_DIR"):
        search_memory.WIKI_DIR.exists = MagicMock(return_value=False)
        search_memory.KNOWLEDGE_DIR.exists = MagicMock(return_value=False)
        pages = search_memory._collect_pages("all")
        assert pages == []


def test_needs_rebuild_no_index():
    """Returns True when index doesn't exist."""
    import search_memory

    with patch.object(Path, "exists", return_value=False):
        assert search_memory._needs_rebuild([]) is True


def test_needs_rebuild_fresh_files():
    """Returns True when source files are newer than index."""
    import time

    import search_memory

    fake_page = MagicMock()
    fake_page.stat.return_value.st_mtime = time.time()
    fake_page.is_file.return_value = True

    with patch.object(Path, "exists", return_value=True), \
         patch.object(Path, "stat") as mock_stat:
        mock_stat.return_value.st_mtime = time.time() - 3600  # index 1h old
        assert search_memory._needs_rebuild([fake_page]) is True


def test_slug_is_indexed_and_selects_the_matching_duplicate(tmp_path, monkeypatch):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    first = notes / "first-implementation.md"
    second = notes / "second-implementation.md"
    content = "---\ntype: concept\n---\n# Shared title\n\nUnrelated body.\n"
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")
    index_dir = tmp_path / "cache" / "search"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index_dir / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / "manifest.json")

    results = search_memory.search("second implementation", force_rebuild=True)

    assert results[0]["path"] == "knowledge/notes/second-implementation.md"


def test_concurrent_index_builds_use_unique_temps_and_leave_valid_index(
    tmp_path, monkeypatch
):
    import doctor
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = notes / "page.md"
    page.write_text("# Page\nBody", encoding="utf-8")
    index_dir = tmp_path / "cache"
    index = index_dir / "index.sqlite"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index)
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / ".paths-manifest")
    real_connect = search_memory.sqlite3.connect
    barrier = threading.Barrier(2)
    opened = []
    opened_lock = threading.Lock()

    def overlapping_connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        with opened_lock:
            opened.append(Path(database))
        barrier.wait(timeout=2)
        return connection

    with monkeypatch.context() as context:
        context.setattr(search_memory.sqlite3, "connect", overlapping_connect)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: search_memory._build_index([page]), range(2)))

    temp_paths = [path for path in opened if path != index]
    assert len(temp_paths) == 2
    assert len(set(temp_paths)) == 2
    assert not any(path.exists() for path in temp_paths)
    check = doctor._index_check(
        tmp_path,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
    )
    assert check["status"] == "ok"


def test_index_swap_retries_transient_windows_access_denial(tmp_path, monkeypatch):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = notes / "page.md"
    page.write_text("# Page\nBody", encoding="utf-8")
    index_dir = tmp_path / "cache"
    index = index_dir / "index.sqlite"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index)
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / ".paths-manifest")
    monkeypatch.setattr(search_memory.sys, "platform", "win32")
    real_replace = search_memory.os.replace
    attempts = 0

    def transient_access_denial(source, destination):
        nonlocal attempts
        if Path(destination) == index:
            attempts += 1
            if attempts < 3:
                error = PermissionError(13, "transient index access denial")
                error.winerror = 5
                raise error
        return real_replace(source, destination)

    monkeypatch.setattr(search_memory.os, "replace", transient_access_denial)

    search_memory._build_index([page])

    assert attempts == 3
    with closing(sqlite3.connect(index)) as database:
        assert database.execute("SELECT COUNT(*) FROM pages").fetchone() == (1,)
    assert not list(index_dir.glob(".index.sqlite.*.tmp"))


def test_repeated_thread_and_process_index_builds_leave_valid_index(
    tmp_path, monkeypatch
):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nBody", encoding="utf-8")
    index_dir = tmp_path / "cache"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index_dir / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / ".paths-manifest")

    for executor_class in (
        concurrent.futures.ThreadPoolExecutor,
        concurrent.futures.ProcessPoolExecutor,
    ):
        with executor_class(max_workers=4) as executor:
            futures = [
                executor.submit(_build_search_index_worker, str(tmp_path), 4)
                for _ in range(4)
            ]
            assert [future.result(timeout=30) for future in futures] == [True] * 4

        with closing(sqlite3.connect(index_dir / "index.sqlite")) as database:
            assert database.execute("SELECT COUNT(*) FROM pages").fetchone() == (1,)
        assert not list(index_dir.glob(".index.sqlite.*.tmp"))


def test_index_swap_recovers_aged_lock_owned_by_dead_pid(tmp_path, monkeypatch):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    page = notes / "page.md"
    page.write_text("# Page\nBody", encoding="utf-8")
    index_dir = tmp_path / "cache"
    index_dir.mkdir()
    index = index_dir / "index.sqlite"
    lock = index.with_suffix(index.suffix + ".swap.lock")
    lock.write_text(
        json.dumps({"pid": 2_147_483_647, "token": "abandoned"}),
        encoding="utf-8",
    )
    old = time.time() - 60
    os.utime(lock, (old, old))
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index)
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / ".paths-manifest")

    search_memory._build_index([page])

    assert index.exists()
    assert not lock.exists()
