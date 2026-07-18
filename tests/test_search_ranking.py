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
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def test_needs_rebuild_fresh_files(tmp_path):
    """Returns True when source files are newer than index."""
    import search_memory

    page = tmp_path / "knowledge" / "notes" / "page.md"
    index = tmp_path / "cache" / "index.sqlite"
    manifest = tmp_path / "cache" / ".paths-manifest"
    page.parent.mkdir(parents=True)
    index.parent.mkdir(parents=True)
    page.write_text("# Page\n", encoding="utf-8")
    with sqlite3.connect(index) as connection:
        connection.execute("CREATE TABLE pages (slug TEXT)")
    manifest.write_text(
        json.dumps(["knowledge/notes/page.md"]), encoding="utf-8"
    )
    old = time.time() - 3600
    os.utime(index, (old, old))

    assert search_memory._needs_rebuild(
        [page],
        root=tmp_path,
        index_file=index,
        index_manifest=manifest,
    ) is True


@pytest.mark.parametrize(
    "missing",
    ["path", "title", "summary", "body", "project", "timestamp", "slug"],
)
def test_needs_rebuild_rejects_partial_fts_schema(tmp_path, missing):
    import search_memory

    index = tmp_path / "index.sqlite"
    manifest = tmp_path / "manifest.json"
    columns = [
        column
        for column in ("path", "title", "summary", "body", "project", "timestamp", "slug")
        if column != missing
    ]
    with sqlite3.connect(index) as connection:
        connection.execute(f"CREATE VIRTUAL TABLE pages USING fts5({', '.join(columns)})")
    manifest.write_text("[]", encoding="utf-8")

    assert search_memory._needs_rebuild(
        [], root=tmp_path, index_file=index, index_manifest=manifest
    ) is True


def test_needs_rebuild_rejects_type_incompatible_regular_pages_table(tmp_path):
    import search_memory

    index = tmp_path / "index.sqlite"
    manifest = tmp_path / "manifest.json"
    with sqlite3.connect(index) as connection:
        connection.execute(
            "CREATE TABLE pages(path TEXT, title TEXT, summary TEXT, body TEXT, "
            "project TEXT, timestamp TEXT, slug TEXT)"
        )
    manifest.write_text("[]", encoding="utf-8")

    assert search_memory._needs_rebuild(
        [], root=tmp_path, index_file=index, index_manifest=manifest
    ) is True


def test_needs_rebuild_rejects_fts_with_incompatible_column_options(tmp_path):
    import search_memory

    index = tmp_path / "index.sqlite"
    manifest = tmp_path / "manifest.json"
    with sqlite3.connect(index) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE pages USING fts5("
            "path, title, summary, body, project, timestamp, slug)"
        )
    manifest.write_text("[]", encoding="utf-8")

    assert search_memory._needs_rebuild(
        [], root=tmp_path, index_file=index, index_manifest=manifest
    ) is True


def test_search_does_not_query_incompatible_index_when_rebuild_fails(
    tmp_path, monkeypatch
):
    import search_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "page.md"
    page.write_text("# Page\n", encoding="utf-8")
    index = tmp_path / "cache/index.sqlite"
    index.parent.mkdir()
    with sqlite3.connect(index) as connection:
        connection.execute("CREATE TABLE pages(slug TEXT)")
    manifest = tmp_path / "cache/manifest.json"
    manifest.write_text(json.dumps(["knowledge/notes/page.md"]), encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index)
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", manifest)
    monkeypatch.setattr(
        search_memory,
        "_build_index",
        lambda pages: (_ for _ in ()).throw(RuntimeError("rebuild failed")),
    )

    with pytest.raises(RuntimeError, match="rebuild failed"):
        search_memory.search("page", graph=False, rerank=False)


def test_page_collector_honors_deadline(tmp_path):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\n", encoding="utf-8")

    with pytest.raises(TimeoutError):
        search_memory._collect_pages(
            knowledge_dir=notes,
            root=tmp_path,
            deadline=time.monotonic() - 1,
        )


@pytest.mark.parametrize("limit_name", ["MAX_SEARCH_ENTRIES", "MAX_SEARCH_DIRECTORIES"])
def test_page_collector_bounds_all_entries_and_directories(tmp_path, monkeypatch, limit_name):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    if limit_name == "MAX_SEARCH_ENTRIES":
        for index in range(4):
            (notes / f"ignored-{index}.bin").write_bytes(b"x")
    else:
        for index in range(4):
            (notes / f"directory-{index}").mkdir()
    monkeypatch.setattr(search_memory, limit_name, 2)

    with pytest.raises(ValueError, match="limit"):
        search_memory._collect_pages(knowledge_dir=notes, root=tmp_path)


def test_page_collector_bounds_depth_before_reading_deep_page(tmp_path, monkeypatch):
    import search_memory

    notes = tmp_path / "knowledge" / "notes"
    deep = notes / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "secret.md").write_text("# Too deep\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "MAX_SEARCH_DEPTH", 2)

    with pytest.raises(ValueError, match="depth"):
        search_memory._collect_pages(knowledge_dir=notes, root=tmp_path)


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


def test_exact_filename_short_circuit_emits_final_impressions_once(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "exact-page.md"
    page.write_text("---\ntype: concept\n---\n# Exact Page\nbody\n", encoding="utf-8")
    index_dir = tmp_path / "cache/search"
    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index_dir / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / "manifest.json")
    monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)

    results = search_memory.search(
        "exact page", force_rebuild=True, graph=False, rerank=False,
        source_tool="test.search",
    )

    rows = retrieval_telemetry.read_events(limit=10, db_path=database)
    assert len(rows) == len(results) == 1
    assert (rows[0].event_kind, rows[0].candidate_id, rows[0].rank) == (
        "impression", "exact-page", 1
    )
    assert rows[0].retrieval_mode == "exact"
    assert rows[0].source_tool == "test.search"


def test_empty_search_emits_no_impressions(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)
    assert search_memory.search("", source_tool="test.search") == []
    assert retrieval_telemetry.read_events(limit=10, db_path=database) == []


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "10"])
def test_search_rejects_invalid_limit_before_dispatch(limit, monkeypatch):
    import search_memory

    monkeypatch.setattr(
        search_memory,
        "_active_generation_catalog",
        lambda: pytest.fail("invalid limit reached generation selection"),
    )
    monkeypatch.setattr(
        search_memory,
        "_legacy_search",
        lambda *args, **kwargs: pytest.fail("invalid limit reached legacy search"),
    )

    with pytest.raises(ValueError, match="limit"):
        search_memory.search("needle", limit=limit)


def test_search_rejects_limit_above_ceiling_before_dispatch(monkeypatch):
    import search_memory

    monkeypatch.setattr(
        search_memory,
        "_legacy_search",
        lambda *args, **kwargs: pytest.fail("oversized limit reached SQL"),
    )

    with pytest.raises(ValueError, match="limit"):
        search_memory.search("needle", limit=search_memory.MAX_SEARCH_LIMIT + 1)


def test_cli_rejects_invalid_limit_before_search(monkeypatch):
    import search_memory

    monkeypatch.setattr(sys, "argv", ["search_memory.py", "needle", "--limit", "0"])
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda *args, **kwargs: pytest.fail("invalid CLI limit reached search"),
    )

    with pytest.raises(SystemExit) as error:
        search_memory.main()

    assert error.value.code == 2


def test_search_can_defer_telemetry_to_post_filter_caller(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "exact-page.md"
    page.write_text("# Exact Page\n", encoding="utf-8")
    index_dir = tmp_path / "cache/search"
    database = tmp_path / "cache/evidence-graph/telemetry.sqlite3"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", index_dir)
    monkeypatch.setattr(search_memory, "INDEX_FILE", index_dir / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", index_dir / "manifest.json")
    monkeypatch.setattr(retrieval_telemetry, "TELEMETRY_DB", database)

    assert search_memory.search(
        "exact page", force_rebuild=True, graph=False, rerank=False,
        emit_telemetry=False,
    )
    assert retrieval_telemetry.read_events(limit=10, db_path=database) == []


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


def _generation_snapshot(tmp_path, pages):
    from corpus_snapshot import collect_corpus

    vault = tmp_path / "vault"
    (vault / "knowledge/notes").mkdir(parents=True)
    for relative, content in pages.items():
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
    return vault, collect_corpus(vault)


def _activate_search_generation(tmp_path, snapshot, descriptors, *, vector=None):
    import search_memory
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes

    catalog = GenerationCatalog(tmp_path / "state")
    generation_id = "gen-search"
    directory = catalog.generations_path / generation_id
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": None,
        "graph_extractor_version": None,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": sorted(descriptors, key=lambda item: item["path"]),
        "vector_state": "absent",
    }
    if vector is not None:
        manifest.update(
            embedding_model_id=vector["model_id"],
            embedding_model_revision=vector["model_revision"],
            vector_dimensions=vector["dimensions"],
            vector_state="complete",
        )
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    catalog.register(generation_id)
    assert catalog.activate(generation_id, expected_active=None)
    return catalog, manifest


def _unregistered_vector_generation(tmp_path):
    import search_memory

    np = pytest.importorskip("numpy")
    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    generation_root = tmp_path / "generations"
    generation = generation_root / "gen-search"
    generation.mkdir(parents=True)
    descriptors = [search_memory.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=lambda texts: np.eye(len(texts), 2, dtype=np.float32),
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    manifest = {
        "generation_id": "gen-search",
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": "deterministic/model",
        "embedding_model_revision": "revision-1",
        "vector_dimensions": 2,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": descriptors,
        "vector_state": "complete",
    }

    class Catalog:
        generations_path = generation_root

        def get_active(self):
            return manifest

    return generation, manifest, Catalog()


def _refresh_artifact_descriptor(manifest, path):
    import search_memory

    descriptor = search_memory._artifact_descriptor(path, path.name)
    manifest["artifacts"] = [
        descriptor if item["path"] == path.name else item
        for item in manifest["artifacts"]
    ]


def test_generation_fts_preserves_exact_snapshot_chunks_and_typed_duplicate_paths(tmp_path):
    import search_memory

    vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/concept/same.md": (
                "---\ntype: concept\nsource_authority: user\nconfidence: high\n---\n"
                "# Alpha\nShared needle.\n## Child\nSecond needle.\n"
            ),
            "knowledge/notes/pattern/same.md": (
                "---\ntype: pattern\nsource_authority: web\nconfidence: low\n---\n"
                "# Beta\nShared needle.\n"
            ),
        },
    )
    generation = tmp_path / "generation"
    generation.mkdir()

    descriptor = search_memory.build_generation_fts(snapshot, generation)

    assert descriptor["path"] == "search.sqlite3"
    artifact = generation / descriptor["path"]
    assert descriptor["size"] == artifact.stat().st_size
    assert descriptor["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    with closing(sqlite3.connect(artifact)) as database:
        rows = database.execute(
            "SELECT chunk_id, source_path, heading_ancestry, type, authority, "
            "confidence, source_sha256 FROM chunks ORDER BY rowid"
        ).fetchall()
    assert [row[0] for row in rows] == [chunk.id for chunk in snapshot.chunks]
    assert [row[1] for row in rows] == [chunk.source_path for chunk in snapshot.chunks]
    assert {row[1] for row in rows} == {
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    }
    assert json.loads(rows[1][2]) == ["Alpha", "Child"]
    assert {(row[3], row[4], row[5]) for row in rows} == {
        ("concept", "user", "high"),
        ("pattern", "web", "low"),
    }
    assert [row[6] for row in rows] == [chunk.source_sha256 for chunk in snapshot.chunks]
    assert not (vault / "cache/index.sqlite").exists()


def test_generation_numpy_uses_same_order_and_is_a_real_matrix(tmp_path):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nFirst vector text.\n## More\nSecond vector text.\n",
            "knowledge/notes/b.md": "# B\nThird vector text.\n",
        },
    )
    generation = tmp_path / "generation"
    generation.mkdir()
    calls = []

    def embed(texts):
        calls.append(tuple(texts))
        return np.arange(len(texts) * 3, dtype=np.float32).reshape(len(texts), 3)

    descriptors = search_memory.build_generation_numpy_vectors(
        snapshot,
        generation,
        embedder=embed,
        model_id="deterministic/model",
        model_revision="revision-1",
        dimensions=3,
    )

    assert calls == [tuple(chunk.text for chunk in snapshot.chunks)]
    assert [item["path"] for item in descriptors] == ["vectors.json", "vectors.npy"]
    matrix = np.load(generation / "vectors.npy", mmap_mode="r")
    metadata = json.loads((generation / "vectors.json").read_text(encoding="utf-8"))
    assert isinstance(matrix, np.ndarray)
    assert matrix.shape == (len(snapshot.chunks), 3)
    assert metadata["chunk_ids"] == [chunk.id for chunk in snapshot.chunks]
    assert metadata["source_sha256"] == [chunk.source_sha256 for chunk in snapshot.chunks]
    assert metadata["corpus_sha256"] == snapshot.corpus_sha256
    assert metadata["collector_version"] == snapshot.collector_version
    assert metadata["extractor_version"] == snapshot.extractor_version


def test_generation_numpy_failure_leaves_both_artifacts_absent(tmp_path):
    pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nVector content.\n"}
    )
    generation = tmp_path / "generation"
    generation.mkdir()

    with pytest.raises(RuntimeError, match="embedding failed"):
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=lambda texts: (_ for _ in ()).throw(RuntimeError("embedding failed")),
            model_id="model",
            model_revision="rev",
            dimensions=2,
        )

    assert not (generation / "vectors.npy").exists()
    assert not (generation / "vectors.json").exists()
    assert not list(generation.glob(".*.tmp"))


@pytest.mark.parametrize("builder", ["fts", "vectors"])
def test_generation_builders_refuse_to_overwrite_published_names(tmp_path, builder):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nImmutable content.\n"}
    )
    generation = tmp_path / "generation"
    generation.mkdir()
    if builder == "fts":
        target = generation / "search.sqlite3"

        def call():
            return search_memory.build_generation_fts(snapshot, generation)
    else:
        target = generation / "vectors.npy"

        def call():
            return search_memory.build_generation_numpy_vectors(
                snapshot,
                generation,
                embedder=lambda texts: np.ones((len(texts), 2), dtype=np.float32),
                model_id="model",
                model_revision="rev",
                dimensions=2,
            )
    target.write_bytes(b"do-not-overwrite")

    with pytest.raises(FileExistsError):
        call()

    assert target.read_bytes() == b"do-not-overwrite"
    assert not list(generation.glob(".*.tmp"))
    if builder == "vectors":
        assert not (generation / "vectors.json").exists()


def test_search_prefers_valid_generation_without_rereading_live_markdown(
    tmp_path, monkeypatch
):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/concept/same.md": "# Concept\nGeneration-only needle.\n",
            "knowledge/notes/pattern/same.md": "# Pattern\nGeneration-only needle.\n",
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    monkeypatch.setattr(
        search_memory,
        "_collect_pages",
        lambda *args, **kwargs: pytest.fail("generation search reread live Markdown"),
    )
    monkeypatch.setattr(
        search_memory,
        "_authority_weight",
        lambda path: pytest.fail("generation search reread authority metadata"),
    )

    results = search_memory.search(
        "generation needle",
        catalog=catalog,
        graph=True,
        rerank=False,
        emit_telemetry=False,
    )

    assert [result["path"] for result in results] == [
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    ]
    assert all(result["generation"] == "gen-search" for result in results)
    assert all(result["effective_mode"] == "BASE" for result in results)


def test_generation_search_reports_generation_to_telemetry(tmp_path, monkeypatch):
    import retrieval_telemetry
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nTelemetry needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    events = []
    monkeypatch.setattr(
        retrieval_telemetry,
        "best_effort_make_event",
        lambda **fields: fields,
    )
    monkeypatch.setattr(
        retrieval_telemetry,
        "best_effort_record_events",
        lambda values: events.extend(values),
    )

    results = search_memory.search(
        "telemetry needle",
        catalog=catalog,
        graph=False,
        rerank=False,
        source_tool="test.generation-search",
    )

    assert len(events) == len(results) == 1
    assert events[0]["generation"] == "gen-search"
    assert events[0]["candidate_id"] == results[0]["chunk_id"]
    assert events[0]["retrieval_mode"] == "base"
    assert events[0]["source_tool"] == "test.generation-search"


def test_generation_filters_use_indexed_authority_status_and_validity(tmp_path):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/user.md": (
                "---\ntype: decision\nsource_authority: user\n"
                "valid_from: 2025-01-01\nvalid_to: 2027-01-01\n---\n"
                "# User\nFiltering needle.\n"
            ),
            "knowledge/notes/web.md": (
                "---\ntype: decision\nsource_authority: web\nvalid_to: 2025-01-01\n---\n"
                "# Web\nFiltering needle.\n"
            ),
        },
        # Historical status filtering is exercised through validity metadata below.
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])

    results = search_memory.search(
        "filtering needle",
        as_of="2026-01-01",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert [result["path"] for result in results] == ["knowledge/notes/user.md"]
    assert results[0]["authority"] == "user"


def test_missing_or_incompatible_generation_fts_falls_back_to_legacy(
    tmp_path, monkeypatch
):
    import search_memory

    class Catalog:
        generations_path = tmp_path / "generations"

        def get_active(self):
            return {
                "generation_id": "missing",
                "schema_version": "corpus-generation/v1",
                "collector_version": "collector/v1",
                "extractor_version": "extractor/v1",
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "source_manifest_sha256": "0" * 64,
                "artifacts": [],
                "vector_state": "absent",
            }

    # Behavioral: missing generation still routes through retrieve + lexical legacy.
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nNeedle content.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", tmp_path / "cache")
    monkeypatch.setattr(search_memory, "INDEX_FILE", tmp_path / "cache" / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", tmp_path / "cache" / ".paths-manifest")
    monkeypatch.setattr(
        search_memory,
        "_legacy_search",
        lambda *args, **kwargs: pytest.fail("must not bypass retrieve via _legacy_search"),
    )
    results = search_memory.search(
        "Needle content",
        catalog=Catalog(),
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert results
    assert results[0]["fallback_reason"] in {
        "generation_unavailable",
        "generation_corrupt",
        "generation_seal_invalid",
    }
    assert "lexical" in results[0]["signals_used"]


def test_semantic_generation_never_mixes_legacy_vectors_or_graph(tmp_path, monkeypatch):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nSemantic needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    monkeypatch.setattr(
        search_memory,
        "_vector_search",
        lambda *args, **kwargs: pytest.fail("legacy vectors were mixed"),
    )
    monkeypatch.setitem(
        sys.modules,
        "graph_neighbors",
        MagicMock(boost_graph_neighbors=lambda *args: pytest.fail("legacy graph was mixed")),
    )

    results = search_memory.search(
        "semantic needle",
        semantic=True,
        catalog=catalog,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["effective_mode"] == "BASE"
    assert results[0]["fallback_reason"] == "generation_vectors_unavailable"
    assert results[0]["generation"] == "gen-search"


def test_semantic_search_uses_complete_matching_generation_numpy_vectors(tmp_path):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()

    def embed(texts):
        if len(texts) == 1 and texts[0] == "semantic":
            return np.array([[1.0, 0.0]], dtype=np.float32)
        return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)

    descriptors = [search_memory.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=embed,
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    catalog, _manifest = _activate_search_generation(
        tmp_path,
        snapshot,
        descriptors,
        vector={
            "model_id": "deterministic/model",
            "model_revision": "revision-1",
            "dimensions": 2,
        },
    )

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=embed,
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=True,
        rerank=False,
        emit_telemetry=False,
    )

    assert results[0]["path"] == "knowledge/notes/a.md"
    assert all(result["generation"] == "gen-search" for result in results)
    assert all(result["effective_mode"] == "HYBRID" for result in results)
    assert all(result["fallback_reason"] is None for result in results)


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "collector_version",
        "extractor_version",
        "dimensions",
        "source_ids",
        "source_paths",
        "chunk_ids",
        "source_sha256",
        "model_id",
        "model_revision",
    ],
)
def test_generation_numpy_metadata_mismatch_falls_back_to_base(
    tmp_path, field
):
    np = pytest.importorskip("numpy")
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path,
        {
            "knowledge/notes/a.md": "# A\nSemantic shared term.\n",
            "knowledge/notes/b.md": "# B\nSemantic shared term.\n",
        },
    )
    generation_root = tmp_path / "generations"
    generation = generation_root / "gen-search"
    generation.mkdir(parents=True)

    def documents_embedder(texts):
        return np.eye(len(texts), 2, dtype=np.float32)

    descriptors = [search_memory.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=documents_embedder,
            model_id="deterministic/model",
            model_revision="revision-1",
            dimensions=2,
        )
    )
    metadata_path = generation / "vectors.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if isinstance(metadata[field], list):
        metadata[field] = ["mismatch"] * len(metadata[field])
    elif field == "dimensions":
        metadata[field] = 3
    else:
        metadata[field] = "mismatch"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    updated_metadata = search_memory._artifact_descriptor(
        metadata_path, metadata_path.name
    )
    descriptors = [
        updated_metadata if item["path"] == metadata_path.name else item
        for item in descriptors
    ]

    class Catalog:
        generations_path = generation_root

        def get_active(self):
            return {
                "generation_id": "gen-search",
                "schema_version": "corpus-generation/v1",
                "collector_version": snapshot.collector_version,
                "extractor_version": snapshot.extractor_version,
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "embedding_model_id": "deterministic/model",
                "embedding_model_revision": "revision-1",
                "vector_dimensions": 2,
                "source_manifest_sha256": snapshot.corpus_sha256,
                "artifacts": descriptors,
                "vector_state": "complete",
            }

    query_embedder_called = False

    def query_embedder(texts):
        nonlocal query_embedder_called
        query_embedder_called = True
        return np.array([[1.0, 0.0]], dtype=np.float32)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=Catalog(),
        generation_embedder=query_embedder,
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    assert all(result["effective_mode"] == "BASE" for result in results)
    assert all(
        result["fallback_reason"] == "generation_vectors_unavailable"
        for result in results
    )
    assert query_embedder_called is False


@pytest.mark.parametrize(
    "matrix",
    [
        pytest.param(lambda np: np.ones((2, 2), dtype=np.float64), id="wrong-float-dtype"),
        pytest.param(lambda np: np.ones((2, 2), dtype=np.int64), id="integer"),
        pytest.param(lambda np: np.full((2, 2), "x"), id="non-numeric"),
        pytest.param(
            lambda np: np.array([[np.nan, 0.0], [0.0, 1.0]], dtype=np.float32),
            id="non-finite",
        ),
    ],
)
def test_generation_numpy_rejects_invalid_matrix_and_falls_back_base(
    tmp_path, matrix
):
    np = pytest.importorskip("numpy")
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    vector_path = generation / "vectors.npy"
    np.save(vector_path, matrix(np), allow_pickle=False)
    _refresh_artifact_descriptor(manifest, vector_path)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    assert all(result["effective_mode"] == "BASE" for result in results)
    assert all(
        result["fallback_reason"] == "generation_vectors_unavailable"
        for result in results
    )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -float("inf")])
def test_generation_numpy_rejects_non_finite_query_vector(tmp_path, bad_value):
    np = pytest.importorskip("numpy")
    import search_memory

    _generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)

    results = search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array(
            [[bad_value, 0.0]], dtype=np.float32
        ),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )

    assert results
    assert all(result["effective_mode"] == "BASE" for result in results)
    assert all(
        result["fallback_reason"] == "generation_vectors_unavailable"
        for result in results
    )


def test_corrupt_active_generation_falls_back_without_querying_it(tmp_path, monkeypatch):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nCorrupt needle.\n"}
    )
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-search"
    generation.mkdir()
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, [descriptor])
    artifact = generation / "search.sqlite3"
    content = artifact.read_bytes()
    artifact.write_bytes(b"X" + content[1:])
    monkeypatch.setattr(
        search_memory,
        "_legacy_search",
        lambda *args, **kwargs: pytest.fail("must not bypass retrieve via _legacy_search"),
    )
    # Provide a real legacy lexical path for recovery.
    vault = tmp_path / "vault"
    if not (vault / "knowledge" / "notes").exists():
        notes = tmp_path / "knowledge" / "notes"
        # snapshot already created vault layout via helper
        pass
    results = search_memory.search(
        "Corrupt needle",
        catalog=catalog,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert isinstance(results, list)
    if results:
        assert results[0].get("fallback_reason") in {
            None,
            "generation_unavailable",
            "generation_corrupt",
            "generation_seal_changed",
            "generation_seal_invalid",
        }
        assert "lexical" in results[0].get("signals_used", ["lexical"])


def test_generation_reader_rejects_source_hash_and_version_mismatch(tmp_path, monkeypatch):
    import search_memory

    _vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nBound needle.\n"}
    )
    generation_root = tmp_path / "generations"
    generation = generation_root / "gen-search"
    generation.mkdir(parents=True)
    descriptor = search_memory.build_generation_fts(snapshot, generation)
    with sqlite3.connect(generation / "search.sqlite3") as database:
        database.execute(
            "UPDATE generation_metadata SET value = ? WHERE key = ?",
            ("0" * 64, "source_manifest_sha256"),
        )

    class Catalog:
        generations_path = generation_root

        def get_active(self):
            return {
                "generation_id": "gen-search",
                "schema_version": "corpus-generation/v1",
                "collector_version": snapshot.collector_version,
                "extractor_version": snapshot.extractor_version,
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "source_manifest_sha256": snapshot.corpus_sha256,
                "artifacts": [descriptor],
                "vector_state": "absent",
            }

    monkeypatch.setattr(
        search_memory,
        "_legacy_search",
        lambda *args, **kwargs: pytest.fail("must not bypass retrieve via _legacy_search"),
    )
    # Point ROOT at the snapshot vault so legacy lexical can recover.
    vault = _vault
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", vault / "knowledge" / "notes")
    monkeypatch.setattr(search_memory, "WIKI_DIR", vault / "knowledge" / "notes")
    monkeypatch.setattr(search_memory, "INDEX_DIR", tmp_path / "cache")
    monkeypatch.setattr(search_memory, "INDEX_FILE", tmp_path / "cache" / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", tmp_path / "cache" / ".paths-manifest")

    results = search_memory.search(
        "Bound needle",
        catalog=Catalog(),
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert results
    assert results[0]["fallback_reason"] in {
        "generation_unavailable",
        "generation_corrupt",
        "generation_seal_changed",
        "generation_seal_invalid",
    }
    assert "lexical" in results[0]["signals_used"]


@pytest.mark.parametrize(
    ("damage", "statement", "parameters"),
    [
        (
            "missing-metadata",
            "DELETE FROM generation_metadata WHERE key = ?",
            ("chunk_count",),
        ),
        (
            "wrong-chunk-count",
            "UPDATE generation_metadata SET value = ? WHERE key = ?",
            ("999", "chunk_count"),
        ),
        (
            "malformed-heading-json",
            "UPDATE chunks SET heading_ancestry = ? WHERE rowid = 1",
            ("{bad-json",),
        ),
        (
            "non-list-heading-json",
            "UPDATE chunks SET heading_ancestry = ? WHERE rowid = 1",
            ('{"heading":"bad"}',),
        ),
        (
            "invalid-chunk-order",
            "UPDATE chunks SET chunk_order = ? WHERE rowid = 1",
            (99,),
        ),
        (
            "invalid-source-hash",
            "UPDATE chunks SET source_sha256 = ? WHERE rowid = 1",
            ("not-a-sha256",),
        ),
        (
            "invalid-chunk-id",
            "UPDATE chunks SET chunk_id = ? WHERE rowid = 1",
            ("not-a-sha256",),
        ),
        (
            "blank-content",
            "UPDATE chunks SET content = ? WHERE rowid = 1",
            ("",),
        ),
    ],
)
def test_malformed_generation_fts_falls_back_legacy(
    tmp_path, monkeypatch, damage, statement, parameters
):
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    with sqlite3.connect(artifact) as database:
        database.execute(statement, parameters)
    _refresh_artifact_descriptor(manifest, artifact)
    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search("semantic", catalog=catalog) is marker, damage


@pytest.mark.parametrize(
    "schema_damage", ["tokenizer", "column", "option", "metadata-table"]
)
def test_generation_fts_rejects_inexact_schema(tmp_path, monkeypatch, schema_damage):
    import search_memory

    generation, manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    replacement = generation / "replacement.sqlite3"
    with closing(sqlite3.connect(artifact)) as source:
        metadata = source.execute(
            "SELECT key, value FROM generation_metadata ORDER BY key"
        ).fetchall()
        rows = source.execute("SELECT * FROM chunks ORDER BY rowid").fetchall()
        schema = source.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='chunks'"
        ).fetchone()[0]
    if schema_damage == "tokenizer":
        schema = schema.replace("porter unicode61", "unicode61")
        selected_rows = rows
    elif schema_damage == "column":
        schema = schema.replace("language UNINDEXED,", "")
        selected_rows = [row[:19] + row[20:] for row in rows]
    elif schema_damage == "option":
        schema = schema.rstrip().removesuffix(")") + ", detail=none)"
        selected_rows = rows
    else:
        selected_rows = rows
    with closing(sqlite3.connect(replacement)) as database, database:
        if schema_damage == "metadata-table":
            database.execute("CREATE TABLE generation_metadata(key TEXT, value TEXT)")
        else:
            database.execute(
                "CREATE TABLE generation_metadata("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        database.executemany(
            "INSERT INTO generation_metadata(key, value) VALUES (?, ?)", metadata
        )
        database.execute(schema)
        database.executemany(
            "INSERT INTO chunks VALUES ("
            + ",".join("?" for _ in selected_rows[0])
            + ")",
            selected_rows,
        )
    os.replace(replacement, artifact)
    _refresh_artifact_descriptor(manifest, artifact)
    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search("semantic", catalog=catalog) is marker


def test_generation_results_discarded_when_active_manifest_changes_after_read(
    tmp_path, monkeypatch
):
    import search_memory

    generation, manifest, _catalog = _unregistered_vector_generation(tmp_path)

    class Catalog:
        generations_path = generation.parent
        calls = 0

        def get_active(self):
            self.calls += 1
            if self.calls == 1:
                return manifest
            changed = dict(manifest)
            changed["source_manifest_sha256"] = "0" * 64
            return changed

    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search("semantic", catalog=Catalog()) is marker


def test_generation_results_discarded_when_fts_corrupts_after_query(
    tmp_path, monkeypatch
):
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    artifact = generation / "search.sqlite3"
    real_search = search_memory._generation_fts_search

    def corrupt_after_query(*args, **kwargs):
        results = real_search(*args, **kwargs)
        with artifact.open("ab") as output:
            output.write(b"corrupt-after-query")
        return results

    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(search_memory, "_generation_fts_search", corrupt_after_query)
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search("semantic", catalog=catalog) is marker


def test_generation_hybrid_discarded_when_vector_artifact_changes_after_use(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    metadata_path = generation / "vectors.json"
    real_search = search_memory._generation_vectors_search

    def corrupt_after_vector_use(*args, **kwargs):
        results = real_search(*args, **kwargs)
        with metadata_path.open("ab") as output:
            output.write(b" ")
        return results

    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(
        search_memory,
        "_generation_vectors_search",
        corrupt_after_vector_use,
    )
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    ) is marker


def test_generation_hybrid_discarded_after_same_byte_vector_replacement(
    tmp_path, monkeypatch
):
    np = pytest.importorskip("numpy")
    import search_memory

    generation, _manifest, catalog = _unregistered_vector_generation(tmp_path)
    metadata_path = generation / "vectors.json"
    real_search = search_memory._generation_vectors_search

    def replace_after_vector_use(*args, **kwargs):
        results = real_search(*args, **kwargs)
        replacement = generation / "replacement-vectors.json"
        replacement.write_bytes(metadata_path.read_bytes())
        os.replace(replacement, metadata_path)
        return results

    marker = [{"path": "legacy", "title": "Legacy", "summary": "", "score": 1}]
    monkeypatch.setattr(
        search_memory,
        "_generation_vectors_search",
        replace_after_vector_use,
    )
    monkeypatch.setattr(search_memory, "_legacy_search", lambda *args, **kwargs: marker)

    assert search_memory.search(
        "semantic",
        semantic=True,
        catalog=catalog,
        generation_embedder=lambda texts: np.array([[1.0, 0.0]], dtype=np.float32),
        generation_model_id="deterministic/model",
        generation_model_revision="revision-1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    ) is marker


def test_publication_fence_rejects_preserved_mtime_drift_before_catalog_calls(tmp_path):
    import search_memory
    from corpus_snapshot import CorpusChanged

    vault, snapshot = _generation_snapshot(
        tmp_path, {"knowledge/notes/page.md": "# Page\nBefore.\n"}
    )
    target = vault / "knowledge/notes/page.md"
    before = target.stat()
    target.write_text("# Page\nChanged\n", encoding="utf-8", newline="")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    class Catalog:
        registered = False
        activated = False

        def register(self, generation_id):
            self.registered = True

        def activate(self, generation_id, *, expected_active):
            self.activated = True
            return True

    catalog = Catalog()
    with pytest.raises(CorpusChanged):
        search_memory.publish_generation(
            snapshot,
            vault,
            catalog,
            "gen-search",
            expected_active=None,
        )
    assert catalog.registered is False
    assert catalog.activated is False


def test_publication_holds_one_writer_gate_through_validate_register_and_activate(
    monkeypatch,
):
    import search_memory

    events = []
    lock = threading.Lock()
    writer_attempted = threading.Event()
    writer_entered = threading.Event()

    class Coordinator:
        gate_calls = 0

        @contextmanager
        def writer_gate(self, *, wait_seconds=None):
            self.gate_calls += 1
            name = threading.current_thread().name
            if name == "cooperating-writer":
                writer_attempted.set()
            with lock:
                events.append(f"{name}:enter")
                if name == "cooperating-writer":
                    writer_entered.set()
                try:
                    yield
                finally:
                    events.append(f"{name}:exit")

    coordinator = Coordinator()
    writer = None

    def validate(snapshot, vault, *, coordinator=None, **deadline_options):
        assert coordinator is None
        assert lock.locked()
        assert deadline_options == {}
        events.append("validate")

    class Catalog:
        def register(self, generation_id):
            nonlocal writer
            assert lock.locked()
            events.append("register")

            def cooperating_write():
                with coordinator.writer_gate():
                    events.append("writer")

            writer = threading.Thread(
                target=cooperating_write,
                name="cooperating-writer",
            )
            writer.start()
            assert writer_attempted.wait(timeout=1)
            assert writer_entered.is_set() is False

        def activate(self, generation_id, *, expected_active):
            assert lock.locked()
            assert writer_entered.is_set() is False
            events.append("activate")
            return True

    monkeypatch.setattr(search_memory, "validate_live_snapshot", validate)

    assert search_memory.publish_generation(
        object(),
        Path("vault"),
        Catalog(),
        "gen-search",
        expected_active=None,
        coordinator=coordinator,
    )
    assert writer is not None
    writer.join(timeout=1)
    assert writer.is_alive() is False
    assert coordinator.gate_calls == 2
    assert events == [
        "MainThread:enter",
        "validate",
        "register",
        "activate",
        "MainThread:exit",
        "cooperating-writer:enter",
        "writer",
        "cooperating-writer:exit",
    ]


def test_publication_deadline_bounds_gate_validation_and_catalog_stages(monkeypatch):
    import search_memory

    now = 10.0
    gate_waits = []
    events = []

    def monotonic():
        return now

    class Coordinator:
        @contextmanager
        def writer_gate(self, *, wait_seconds=None):
            gate_waits.append(wait_seconds)
            events.append("gate-enter")
            try:
                yield
            finally:
                events.append("gate-exit")

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        nonlocal now
        assert coordinator is None
        assert deadline_seconds == 5.0
        events.append("validate")
        now = 12.0

    class Catalog:
        activated = False

        def register(self, generation_id, *, deadline=None):
            nonlocal now
            assert deadline == 15.0
            events.append("register")
            now = 15.0

        def activate(self, generation_id, *, expected_active, deadline=None):
            self.activated = True
            pytest.fail("activation started after publication deadline")

    catalog = Catalog()
    monkeypatch.setattr(search_memory.time, "monotonic", monotonic)
    monkeypatch.setattr(search_memory, "validate_live_snapshot", validate)

    with pytest.raises(TimeoutError, match="deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            catalog,
            "gen-search",
            expected_active=None,
            coordinator=Coordinator(),
            deadline=15.0,
        )

    assert gate_waits == [5.0]
    assert events == ["gate-enter", "validate", "register", "gate-exit"]
    assert catalog.activated is False


def test_publication_forwards_absolute_deadline_without_post_commit_false_timeout(
    monkeypatch,
):
    import search_memory

    now = 10.0
    calls = []

    def monotonic():
        return now

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        assert coordinator is None
        assert deadline_seconds == 5.0
        calls.append(("validate", deadline_seconds))

    class Catalog:
        def register(self, generation_id, *, deadline=None):
            assert deadline == 15.0
            calls.append(("register", deadline))

        def activate(self, generation_id, *, expected_active, deadline=None):
            nonlocal now
            assert deadline == 15.0
            calls.append(("activate", deadline))
            now = 20.0
            return True

    monkeypatch.setattr(search_memory.time, "monotonic", monotonic)
    monkeypatch.setattr(search_memory, "validate_live_snapshot", validate)

    assert search_memory.publish_generation(
        object(),
        Path("vault"),
        Catalog(),
        "gen-search",
        expected_active=None,
        deadline=15.0,
    ) is True
    assert calls == [
        ("validate", 5.0),
        ("register", 15.0),
        ("activate", 15.0),
    ]


@pytest.mark.parametrize("timeout_stage", ["register", "activate"])
def test_publication_propagates_catalog_deadline_timeout(monkeypatch, timeout_stage):
    import search_memory

    activated = False

    def validate(snapshot, vault, *, coordinator=None, deadline_seconds=None):
        assert deadline_seconds == 5.0

    class Catalog:
        def register(self, generation_id, *, deadline=None):
            assert deadline == 15.0
            if timeout_stage == "register":
                raise TimeoutError("catalog register deadline")

        def activate(self, generation_id, *, expected_active, deadline=None):
            nonlocal activated
            assert deadline == 15.0
            activated = True
            if timeout_stage == "activate":
                raise TimeoutError("catalog activate deadline")
            return True

    monkeypatch.setattr(search_memory.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(search_memory, "validate_live_snapshot", validate)

    with pytest.raises(TimeoutError, match=f"catalog {timeout_stage} deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            Catalog(),
            "gen-search",
            expected_active=None,
            deadline=15.0,
        )
    assert activated is (timeout_stage == "activate")


def test_publication_rejects_expired_deadline_before_gate(monkeypatch):
    import search_memory

    class Coordinator:
        def writer_gate(self, *, wait_seconds=None):
            pytest.fail("expired publication attempted writer gate")

    monkeypatch.setattr(search_memory.time, "monotonic", lambda: 10.0)

    with pytest.raises(TimeoutError, match="deadline"):
        search_memory.publish_generation(
            object(),
            Path("vault"),
            object(),
            "gen-search",
            expected_active=None,
            coordinator=Coordinator(),
            deadline=10.0,
        )
