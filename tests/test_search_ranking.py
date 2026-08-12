"""Tests for search_memory.py — ranking logic, boosts, RRF fusion.

Locks in:
1. Title boost: exact title match → higher rank than BM25-only
2. Filename short-circuit: exact filename match → rank 1 always
3. Path preference: knowledge/notes/ pages boosted over knowledge/notes/
4. RRF fusion: weighted (BM25=2, Vector=1, Graph=0.5)
5. Project-scoped boost: project-tagged pages x2
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


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


def test_search_collection_and_index_use_shared_sensitive_frontmatter_parser(
    tmp_path,
    monkeypatch,
):
    import search_memory

    root = tmp_path / "vault"
    notes = root / "knowledge" / "notes"
    cache = tmp_path / "cache"
    notes.mkdir(parents=True)

    def page(name: str, metadata: str) -> Path:
        path = notes / name
        path.write_text(
            f"---\ntype: pattern\n{metadata}\n---\n\n# {name}\n\nsearch needle\n",
            encoding="utf-8",
        )
        return path

    active = page("active.md", 'project: "be\\x74a"\nstatus: active')
    page("inactive.md", 'project: beta\nstatus: "super\\x73eded"')
    page("invalid-status.md", 'project: beta\nstatus: "\\qactive"')
    page("invalid-project.md", 'project: "\\qbeta"\nstatus: active')
    monkeypatch.setattr(search_memory, "ROOT", root)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", cache)
    monkeypatch.setattr(search_memory, "INDEX_FILE", cache / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", cache / ".paths-manifest")

    selection = search_memory._collect_note_selection()
    pages = list(selection.paths)
    search_memory._build_index(selection)

    assert pages == [active]
    with sqlite3.connect(search_memory.INDEX_FILE) as connection:
        rows = connection.execute("SELECT path, project FROM pages").fetchall()
    assert rows == [("knowledge/notes/active.md", "beta")]


def test_stdin_query_rejects_oversize_before_search(monkeypatch, capsys):
    import search_memory

    class BoundedOnlyInput(io.StringIO):
        def __init__(self, value: str):
            super().__init__(value)
            self.request_sizes: list[int] = []

        def read(self, size: int = -1) -> str:
            self.request_sizes.append(size)
            assert size > 0, "reader requested an unbounded allocation"
            return super().read(size)

    stream = BoundedOnlyInput("attacker query " + "x" * 256)
    searches: list[str] = []
    monkeypatch.setattr(search_memory, "MAX_STDIN_QUERY_BYTES", 64, raising=False)
    monkeypatch.setattr(
        search_memory,
        "search",
        lambda query, *_args, **_kwargs: searches.append(query) or [],
    )
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(sys, "argv", ["search_memory.py", "--stdin"])

    assert search_memory.main() == 2
    assert "stdin query" in capsys.readouterr().err.lower()
    assert stream.request_sizes and all(size > 0 for size in stream.request_sizes)
    assert searches == []


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


def _retrieval_page(
    path: Path,
    *,
    title: str,
    status: str | None = None,
    authority: str | None = "inferred",
    confidence: str | None = "low",
    project: str | None = None,
    body: str = "shared retrieval needle",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["type: pattern", f'title: "{title}"']
    if status is not None:
        fields.append(f"status: {status}")
    if authority is not None:
        fields.append(f"source_authority: {authority}")
    if confidence is not None:
        fields.append(f"confidence: {confidence}")
    if project is not None:
        fields.append(f"project: {project}")
    path.write_text(
        "---\n"
        + "\n".join(fields)
        + f"\n---\n\n# {title}\n\nOne-sentence summary: {body}.\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _configure_retrieval_modules(monkeypatch, root: Path):
    import rebuild_memory_index
    import search_memory

    notes = root / "knowledge" / "notes"
    cache = root / ".state" / "cache"
    notes.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rebuild_memory_index, "ROOT", root)
    monkeypatch.setattr(rebuild_memory_index, "memory", root / "knowledge")
    monkeypatch.setattr(rebuild_memory_index, "knowledge", notes)
    monkeypatch.setattr(rebuild_memory_index, "out", root / "knowledge" / "index.md")
    monkeypatch.setattr(
        rebuild_memory_index,
        "SUBDIR_SECTIONS",
        {name: notes / path.name for name, path in rebuild_memory_index.SUBDIR_SECTIONS.items()},
    )
    monkeypatch.setattr(search_memory, "ROOT", root)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", cache)
    monkeypatch.setattr(search_memory, "INDEX_FILE", cache / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", cache / ".paths-manifest")
    monkeypatch.setattr(search_memory, "VECTOR_CACHE", cache / "vectors.json")
    return rebuild_memory_index, search_memory, notes


def test_shared_selector_prefers_active_flat_page_over_typed_copy(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    flat = _retrieval_page(
        notes / "same-page.md",
        title="Same Page",
        authority="inferred",
        confidence="low",
    )
    typed = _retrieval_page(
        notes / "patterns" / "same-page.md",
        title="Same Page",
        authority="user",
        confidence="high",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == (flat,)
    assert len(selection.diagnostics) == 1
    assert selection.diagnostics[0].canonical == flat.relative_to(tmp_path).as_posix()
    assert selection.diagnostics[0].shadows == (
        typed.relative_to(tmp_path).as_posix(),
    )


def test_shared_selector_allows_typed_winner_when_flat_page_is_inactive(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    _retrieval_page(
        notes / "same-page.md",
        title="Same Page",
        status="superseded",
        authority="user",
        confidence="high",
    )
    typed = _retrieval_page(
        notes / "patterns" / "same-page.md",
        title="Same Page",
        authority="web",
        confidence="medium",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == (typed,)
    assert selection.diagnostics == ()


def test_typed_duplicate_selection_uses_authority_before_confidence(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    web_low = _retrieval_page(
        notes / "concepts" / "web-copy.md",
        title="Trust Ordering",
        authority="web",
        confidence="low",
    )
    _retrieval_page(
        notes / "patterns" / "ai-copy.md",
        title="Trust Ordering",
        authority="ai-derived",
        confidence="high",
    )
    _retrieval_page(
        notes / "decisions" / "inferred-copy.md",
        title="Trust Ordering",
        authority=None,
        confidence=None,
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == (web_low,)
    [winner] = selection.notes
    assert (winner.authority, winner.confidence) == ("web", "low")


def test_typed_duplicate_selection_uses_confidence_then_lexical_path(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    high = _retrieval_page(
        notes / "z-type" / "confidence-z.md",
        title="Confidence Ordering",
        authority="user",
        confidence="high",
    )
    _retrieval_page(
        notes / "a-type" / "confidence-a.md",
        title="Confidence Ordering",
        authority="user",
        confidence="medium",
    )
    lexical = _retrieval_page(
        notes / "a-type" / "lexical-a.md",
        title="Lexical Ordering",
        authority="user",
        confidence="high",
    )
    _retrieval_page(
        notes / "z-type" / "lexical-z.md",
        title="Lexical Ordering",
        authority="user",
        confidence="high",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert set(selection.paths) == {high, lexical}


def test_selector_defaults_missing_trust_and_rejects_malformed_trust(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    defaulted = _retrieval_page(
        notes / "defaulted.md",
        title="Defaulted Trust",
        authority=None,
        confidence=None,
    )
    malformed_authority = _retrieval_page(
        notes / "malformed-authority.md",
        title="Malformed Authority",
    )
    malformed_authority.write_text(
        malformed_authority.read_text(encoding="utf-8").replace(
            "source_authority: inferred", 'source_authority: "\\qinvalid"'
        ),
        encoding="utf-8",
    )
    malformed_confidence = _retrieval_page(
        notes / "malformed-confidence.md",
        title="Malformed Confidence",
    )
    malformed_confidence.write_text(
        malformed_confidence.read_text(encoding="utf-8").replace(
            "confidence: low", "confidence: impossible"
        ),
        encoding="utf-8",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == (defaulted,)
    [note] = selection.notes
    assert (note.authority, note.confidence) == ("inferred", "low")


def test_duplicate_diagnostics_are_deterministic_and_bounded(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    for group in reversed(range(4)):
        for copy in reversed(range(4)):
            _retrieval_page(
                notes / f"type-{copy}" / f"group-{group}-{copy}.md",
                title=f"Duplicate Group {group}",
                authority="user",
                confidence="high",
            )

    selection = vault_editorial.select_active_notes(
        notes,
        root=tmp_path,
        max_diagnostics=2,
        max_diagnostic_shadows=2,
    )

    assert len(selection.paths) == 4
    assert len(selection.diagnostics) == 2
    assert selection.diagnostics_truncated is True
    assert [item.identity for item in selection.diagnostics] == sorted(
        item.identity for item in selection.diagnostics
    )
    assert all(len(item.shadows) == 2 for item in selection.diagnostics)
    assert all(item.shadows_truncated for item in selection.diagnostics)


def test_selector_excludes_empty_logical_identities_with_bounded_diagnostic(
    tmp_path,
):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    first = _retrieval_page(notes / "patterns" / "!!!.md", title="!!!")
    second = _retrieval_page(notes / "concepts" / "!!!.md", title="!!!")

    selection = vault_editorial.select_active_notes(
        notes,
        root=tmp_path,
        max_diagnostics=1,
        max_diagnostic_shadows=1,
    )

    assert selection.paths == ()
    assert len(selection.diagnostics) == 1
    diagnostic = selection.diagnostics[0]
    assert diagnostic.kind == "invalid-identity"
    assert diagnostic.canonical == ""
    assert diagnostic.shadow_count == 2
    assert diagnostic.shadows in {
        (first.relative_to(tmp_path).as_posix(),),
        (second.relative_to(tmp_path).as_posix(),),
    }
    assert diagnostic.shadows_truncated is True


@pytest.mark.parametrize(
    ("unsafe", "escaped"),
    (
        ("line\u2028separator", r"\u2028"),
        ("paragraph\u2029separator", r"\u2029"),
        ("c1\u0085control", r"\u0085"),
        ("noncharacter\ufdd0", r"\ufdd0"),
    ),
    ids=("line-separator", "paragraph-separator", "c1", "noncharacter"),
)
def test_selector_rejects_unsafe_relative_path_with_escaped_diagnostic(
    tmp_path,
    unsafe,
    escaped,
):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = _retrieval_page(
        notes / f"{unsafe}.md",
        title="Unsafe Relative Path",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == ()
    assert len(selection.diagnostics) == 1
    diagnostic = selection.diagnostics[0]
    assert diagnostic.kind == "unsafe-path"
    assert diagnostic.canonical == ""
    assert diagnostic.shadow_count == 1
    assert len(diagnostic.shadows) == 1
    assert escaped in diagnostic.shadows[0]
    assert unsafe not in diagnostic.shadows[0]
    assert page.exists()


def test_selector_rejects_backtick_that_breaks_source_path_serialization(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = _retrieval_page(
        notes / "tick`name.md",
        title="Backtick Path",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert selection.paths == ()
    assert len(selection.diagnostics) == 1
    diagnostic = selection.diagnostics[0]
    assert diagnostic.kind == "unsafe-path"
    assert diagnostic.shadow_count == 1
    assert page.exists()


def test_selector_and_markdown_index_reject_wikilink_fragment_path(
    tmp_path,
    monkeypatch,
):
    rebuild_memory_index, _search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    import vault_editorial

    unsafe = _retrieval_page(
        notes / "topic#fragment.md",
        title="Unsafe Fragment Topic",
    )
    accepted = _retrieval_page(
        notes / "topic-fragment.md",
        title="Accepted Fragment Topic",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)
    buckets = rebuild_memory_index.collect_pages()
    indexed = {page for pages in buckets.values() for page in pages}
    assert rebuild_memory_index.main() == 0
    rendered = rebuild_memory_index.out.read_text(encoding="utf-8")

    assert selection.paths == (accepted,)
    assert len(selection.diagnostics) == 1
    diagnostic = selection.diagnostics[0]
    assert diagnostic.kind == "unsafe-path"
    assert diagnostic.shadows == (unsafe.relative_to(tmp_path).as_posix(),)
    assert indexed == {accepted}
    assert "[[knowledge/notes/topic#fragment]]" not in rendered
    assert rendered.count("[[knowledge/notes/topic-fragment]]") == 1


@pytest.mark.parametrize(
    "content",
    (
        "---\ntype: pattern\n# Frontmatter Decoy\n---\n\n# Visible Title\n",
        "---\ntype: pattern\n---\n\n```markdown\n# Fence Decoy\n```\n\n# Visible Title\n",
        "---\ntype: pattern\n---\n\n<!--\n# Comment Decoy\n-->\n\n# Visible Title\n",
        "---\ntype: pattern\n---\n\n<script>\n# Raw HTML Decoy\n</script>\n\n# Visible Title\n",
    ),
    ids=("frontmatter", "fence", "comment", "raw-html"),
)
def test_selector_uses_first_visible_h1_for_identity(tmp_path, content):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = notes / "fallback.md"
    page.parent.mkdir(parents=True)
    page.write_text(content, encoding="utf-8")

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    [selected] = selection.notes
    assert selected.title == "Visible Title"


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("<!-- hidden --># Decoy\n\n# Real Title\n", "Real Title"),
        ("# Visible Title <!-- comment -->\n\n# Later Title\n", "Visible Title"),
        ("prefix <!-- hidden --># Decoy\n\n# Real Title\n", "Real Title"),
        ("<!-- hidden\n--># Decoy\n\n# Real Title\n", "Real Title"),
    ),
    ids=(
        "comment-prefix-does-not-create-h1",
        "visible-h1-keeps-title",
        "prose-prefix-does-not-create-h1",
        "comment-close-does-not-create-h1",
    ),
)
def test_selector_recognizes_h1_before_removing_inline_comment_text(
    tmp_path,
    body,
    expected,
):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = notes / "comment-structure.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: pattern\n---\n\n" + body,
        encoding="utf-8",
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    [selected] = selection.notes
    assert selected.title == expected


def test_selector_strips_utf8_bom_before_frontmatter_and_visible_h1_scan(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = notes / "bom.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(
        b"\xef\xbb\xbf---\n"
        b"type: pattern\n"
        b"# Frontmatter Decoy\n"
        b"---\n\n"
        b"# Visible BOM Title\n"
    )

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    [selected] = selection.notes
    assert selected.page_type == "pattern"
    assert selected.title == "Visible BOM Title"
    assert not selected.content.startswith("\ufeff")


def test_selector_counts_stripped_bom_against_raw_aggregate_byte_limit(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = notes / "bom.md"
    page.parent.mkdir(parents=True)
    raw = b"\xef\xbb\xbf---\ntype: pattern\n---\n\n# BOM Title\n"
    page.write_bytes(raw)

    with pytest.raises(OSError, match="aggregate byte limit"):
        vault_editorial.select_active_notes(
            notes,
            root=tmp_path,
            max_total_bytes=len(raw) - 1,
        )


def test_selector_rejects_hardlinked_active_note(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    source = _retrieval_page(
        notes / "source.md",
        title="Hardlinked Source",
    )
    os.link(source, notes / "alias.md")

    with pytest.raises(OSError, match="hard-linked"):
        vault_editorial.select_active_notes(notes, root=tmp_path)


def test_selector_preserves_source_bytes_and_full_file_identity(tmp_path):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    page = _retrieval_page(
        notes / "snapshot.md",
        title="Snapshot Identity",
        body="The immutable selection retains exact validated source bytes.",
    )
    raw = page.read_bytes()
    metadata = page.stat()

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)

    [snapshot] = selection.notes
    assert snapshot.source_bytes == raw
    assert snapshot.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.file_identity.device == metadata.st_dev
    assert snapshot.file_identity.inode == metadata.st_ino
    assert snapshot.file_identity.size == len(raw)
    assert snapshot.file_identity.nlink == 1


def test_selection_generations_track_snapshot_bytes_and_nonwinning_inventory(
    tmp_path,
):
    import vault_editorial

    notes = tmp_path / "knowledge" / "notes"
    winner = _retrieval_page(
        notes / "winner.md",
        title="Generation Winner",
        body="GENERATION-A",
    )
    first = vault_editorial.select_active_notes(notes, root=tmp_path)
    original = winner.stat()
    changed = winner.read_bytes().replace(b"GENERATION-A", b"GENERATION-B")
    winner.write_bytes(changed)
    os.utime(winner, ns=(original.st_atime_ns, original.st_mtime_ns))

    second = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert first.generation.version == 1
    assert first.generation.inventory_sha256 != second.generation.inventory_sha256
    assert first.generation.canonical_sha256 != second.generation.canonical_sha256

    _retrieval_page(
        notes / "patterns" / "shadow.md",
        title="Generation Winner",
        body="A typed shadow does not replace the active flat winner.",
    )
    third = vault_editorial.select_active_notes(notes, root=tmp_path)

    assert second.paths == third.paths == (winner,)
    assert second.generation.inventory_sha256 != third.generation.inventory_sha256
    assert second.generation.canonical_sha256 == third.generation.canonical_sha256


def test_fts_generation_rebuilds_preserved_mtime_content_from_selection(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    page = _retrieval_page(
        notes / "generation.md",
        title="FTS Generation",
        body="oldgenerationneedle",
    )
    first = search_memory._collect_note_selection()
    search_memory._build_index(first)
    original = page.stat()
    page.write_bytes(
        page.read_bytes().replace(
            b"oldgenerationneedle",
            b"newgenerationneedle",
        )
    )
    os.utime(page, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = search_memory._collect_note_selection()

    assert first.generation.canonical_sha256 != second.generation.canonical_sha256
    assert search_memory._needs_rebuild(second) is True
    results = search_memory.search("newgenerationneedle")
    manifest = json.loads(search_memory.INDEX_MANIFEST.read_text(encoding="utf-8"))

    assert [item["path"] for item in results] == [
        page.relative_to(tmp_path).as_posix()
    ]
    assert manifest["version"] == 1
    assert manifest["canonical_sha256"] == second.generation.canonical_sha256


def test_vector_cache_rebuilds_from_snapshot_generation_without_path_reread(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    page = _retrieval_page(
        notes / "vector.md",
        title="Vector Generation",
        body="old vector snapshot",
    )
    calls: list[list[str]] = []

    class EncodedVectors(list):
        def tolist(self):
            return list(self)

    class FakeEmbedder:
        def encode(self, texts, **_kwargs):
            calls.append(list(texts))
            return EncodedVectors([[float(len(text))] for text in texts])

    monkeypatch.setattr(search_memory, "EMBEDDING_DIM", 1)
    monkeypatch.setattr(search_memory, "_get_embedder", lambda: FakeEmbedder())
    first = search_memory._collect_note_selection()
    first_data = search_memory._build_vectors(first)
    original = page.stat()
    page.write_bytes(
        page.read_bytes().replace(
            b"old vector snapshot",
            b"new vector snapshot",
        )
    )
    os.utime(page, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = search_memory._collect_note_selection()

    second_data = search_memory._load_or_build_vectors(second)

    assert first_data is not None and second_data is not None
    assert len(calls) == 2
    assert "old vector snapshot" in calls[0][0]
    assert "new vector snapshot" in calls[1][0]
    assert second_data["generation"]["version"] == 1
    assert (
        second_data["generation"]["canonical_sha256"]
        == second.generation.canonical_sha256
    )


def _vector_cache_payload(search_memory, selection) -> dict:
    titles: list[str] = []
    summaries: list[str] = []
    for note in selection.notes:
        title, summary = search_memory._extract_title_and_summary(
            note.content,
            note.path.stem,
        )
        titles.append(title)
        summaries.append(summary)
    dimensions = search_memory.EMBEDDING_DIM
    return {
        "schema": search_memory.VECTOR_CACHE_SCHEMA,
        "version": search_memory.VECTOR_CACHE_VERSION,
        "generation": search_memory.active_note_generation_manifest(
            selection,
            "vectors-v1",
        ),
        "model": search_memory.EMBEDDING_MODEL,
        "model_version": search_memory.EMBEDDING_MODEL_VERSION,
        "dimensions": dimensions,
        "page_count": len(selection.notes),
        "paths": [note.relative_path for note in selection.notes],
        "hashes": [note.content_sha256 for note in selection.notes],
        "titles": titles,
        "summaries": summaries,
        "projects": [note.project.lower() for note in selection.notes],
        "timestamps": [
            search_memory._timestamp_date(note.content) for note in selection.notes
        ],
        "vectors": [
            [float(index + offset + 1) for offset in range(dimensions)]
            for index, _note in enumerate(selection.notes)
        ],
    }


def _synthetic_vector_selection(notes: Path, page_count: int):
    from types import SimpleNamespace

    content = (
        "---\ntype: pattern\nconfidence: high\nsource_authority: user\n---\n\n"
        "# Vector Page\n\nOne-sentence summary: vector cache snapshot.\n"
    )
    selected = tuple(
        SimpleNamespace(
            path=notes / f"page-{index:04d}.md",
            relative_path=f"knowledge/notes/page-{index:04d}.md",
            content=content,
            content_sha256=f"{index:064x}",
            project="",
        )
        for index in range(page_count)
    )
    return SimpleNamespace(
        notes=selected,
        generation=SimpleNamespace(version=1, canonical_sha256="a" * 64),
    )


def _install_fake_vector_embedder(search_memory, monkeypatch, calls, dimensions=2):
    class EncodedVectors(list):
        def tolist(self):
            return list(self)

    class FakeEmbedder:
        def encode(self, texts, **_kwargs):
            calls.append(list(texts))
            return EncodedVectors(
                [
                    [float(index + offset + 1) for offset in range(dimensions)]
                    for index, _text in enumerate(texts)
                ]
            )

    monkeypatch.setattr(search_memory, "EMBEDDING_DIM", dimensions)
    monkeypatch.setattr(search_memory, "_get_embedder", lambda: FakeEmbedder())


def test_vector_cache_2591_page_payload_roundtrips_through_writer_and_reader(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    page_count = 2_591
    selection = _synthetic_vector_selection(notes, page_count)
    vector_row = [0.0] * search_memory.EMBEDDING_DIM

    class EncodedVectors:
        def tolist(self):
            return [vector_row] * page_count

    class FakeEmbedder:
        def encode(self, texts, **_kwargs):
            assert len(texts) == page_count
            return EncodedVectors()

    monkeypatch.setattr(search_memory, "_get_embedder", lambda: FakeEmbedder())

    built = search_memory._build_vectors(selection)

    assert built is not None
    cache_size = search_memory.VECTOR_CACHE.stat().st_size
    assert 4_300_000 < cache_size < 4_500_000
    del built

    loaded = search_memory._read_vector_cache_payload()

    assert loaded is not None
    assert loaded["page_count"] == page_count
    assert len(loaded["vectors"]) == page_count
    assert search_memory._validate_vector_cache_payload(loaded, selection) is loaded


@pytest.mark.parametrize(("token_delta", "expected_writes"), ((0, 1), (-1, 0)))
def test_vector_cache_writer_uses_reader_lexical_boundary_before_publication(
    tmp_path,
    monkeypatch,
    token_delta,
    expected_writes,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "boundary.md", title="Vector Boundary")
    selection = search_memory._collect_note_selection()
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls, dimensions=2)
    payload = _vector_cache_payload(search_memory, selection)
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    lexical_tokens = sum(char in "{}[],:" for char in encoded)
    writes: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        search_memory,
        "MAX_VECTOR_CACHE_JSON_LEXICAL_TOKENS",
        lexical_tokens + token_delta,
        raising=False,
    )
    monkeypatch.setattr(
        search_memory,
        "atomic_write",
        lambda path, content: writes.append((path, content)),
    )

    built = search_memory._build_vectors(selection)

    assert built is not None
    assert len(writes) == expected_writes


def test_vector_cache_matching_generation_metadata_only_rebuilds(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "metadata-only.md", title="Metadata Only")
    selection = search_memory._collect_note_selection()
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(
        json.dumps(
            {
                "generation": search_memory.active_note_generation_manifest(
                    selection,
                    "vectors-v1",
                )
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls)

    data = search_memory._load_or_build_vectors(selection)

    assert data is not None
    assert data["page_count"] == 1
    assert len(calls) == 1


def test_vector_cache_oversized_matching_generation_uses_bounded_reader_and_rebuilds(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    import vault_editorial

    _retrieval_page(notes / "oversized.md", title="Oversized Cache")
    selection = search_memory._collect_note_selection()
    partial = {
        "generation": search_memory.active_note_generation_manifest(
            selection,
            "vectors-v1",
        )
    }
    raw = json.dumps(partial) + " " * 4_096
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(search_memory, "MAX_VECTOR_CACHE_BYTES", len(raw) - 1, raising=False)
    monkeypatch.setattr(search_memory, "MAX_VECTOR_CACHE_CHARS", len(raw) - 1, raising=False)
    bounded_reads: list[int] = []
    real_read = vault_editorial.read_bounded_note_snapshot

    def tracked_read(path, max_bytes):
        bounded_reads.append(max_bytes)
        return real_read(path, max_bytes)

    monkeypatch.setattr(
        search_memory,
        "read_bounded_note_snapshot",
        tracked_read,
        raising=False,
    )
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls)

    data = search_memory._load_or_build_vectors(selection)

    assert data is not None
    assert bounded_reads == [len(raw) - 1]
    assert len(calls) == 1


def test_vector_cache_deep_matching_generation_rebuilds_before_json_recursion(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "deep-cache.md", title="Deep Cache")
    selection = search_memory._collect_note_selection()
    depth = 64
    raw = (
        '{"generation":'
        + json.dumps(
            search_memory.active_note_generation_manifest(selection, "vectors-v1")
        )
        + ',"nested":'
        + "[" * depth
        + "0"
        + "]" * depth
        + "}"
    )
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(search_memory, "MAX_VECTOR_CACHE_JSON_DEPTH", 8, raising=False)
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls)

    data = search_memory._load_or_build_vectors(selection)

    assert data is not None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field",
    ("schema", "version", "model", "model_version", "dimensions", "page_count"),
)
def test_vector_cache_rebuilds_on_any_exact_metadata_mismatch(
    tmp_path,
    monkeypatch,
    field,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "metadata.md", title="Cache Metadata")
    selection = search_memory._collect_note_selection()
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls)
    payload = _vector_cache_payload(search_memory, selection)
    payload[field] = payload[field] + 1 if type(payload[field]) is int else "wrong"
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(json.dumps(payload), encoding="utf-8")

    data = search_memory._load_or_build_vectors(selection)

    assert data is not None
    assert len(calls) == 1
    assert data[field] != payload[field]


@pytest.mark.parametrize(
    "invalid_kind",
    ("wrong-dim", "nan", "stale-snapshot-hash", "duplicate-path", "extra-key"),
)
def test_vector_cache_rebuilds_invalid_snapshot_entries(
    tmp_path,
    monkeypatch,
    invalid_kind,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "first.md", title="First Cache Page")
    _retrieval_page(notes / "second.md", title="Second Cache Page")
    selection = search_memory._collect_note_selection()
    calls: list[list[str]] = []
    _install_fake_vector_embedder(search_memory, monkeypatch, calls)
    payload = _vector_cache_payload(search_memory, selection)
    if invalid_kind == "wrong-dim":
        payload["vectors"][0] = [1.0]
    elif invalid_kind == "nan":
        payload["vectors"][0][0] = float("nan")
    elif invalid_kind == "stale-snapshot-hash":
        payload["hashes"][0] = "0" * 64
    elif invalid_kind == "duplicate-path":
        payload["paths"][1] = payload["paths"][0]
    else:
        payload["unexpected"] = True
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(json.dumps(payload), encoding="utf-8")

    data = search_memory._load_or_build_vectors(selection)

    assert data is not None
    assert len(calls) == 1
    assert search_memory._validate_vector_cache_payload(data, selection) is not None


def test_vector_cache_invalid_rebuild_payload_fails_controlled(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "controlled.md", title="Controlled Failure")
    selection = search_memory._collect_note_selection()
    partial = {
        "generation": search_memory.active_note_generation_manifest(
            selection,
            "vectors-v1",
        )
    }
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.VECTOR_CACHE.write_text(json.dumps(partial), encoding="utf-8")
    rebuilds: list[int] = []
    monkeypatch.setattr(
        search_memory,
        "_build_vectors",
        lambda _selection: rebuilds.append(1) or partial,
    )

    assert search_memory._load_or_build_vectors(selection) is None
    assert rebuilds == [1]


def test_vector_cache_build_normalizes_vector_conversion_resource_failure(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "conversion.md", title="Conversion Failure")
    selection = search_memory._collect_note_selection()

    class ExhaustedVectors:
        def tolist(self):
            raise MemoryError("injected vector conversion exhaustion")

    class FakeEmbedder:
        def encode(self, _texts, **_kwargs):
            return ExhaustedVectors()

    monkeypatch.setattr(search_memory, "_get_embedder", lambda: FakeEmbedder())

    assert search_memory._build_vectors(selection) is None
    assert not search_memory.VECTOR_CACHE.exists()


def test_markdown_index_renders_one_immutable_selection_without_rereads(
    tmp_path,
    monkeypatch,
):
    rebuild_memory_index, _search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    original_summary = "Snapshot summary remains stable during rendering"
    page = _retrieval_page(
        notes / "markdown.md",
        title="Markdown Snapshot",
        body=original_summary,
    )
    import vault_editorial

    selection = vault_editorial.select_active_notes(notes, root=tmp_path)
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            original_summary,
            "MUTATED CONTENT MUST NOT BE RENDERED",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        rebuild_memory_index,
        "select_active_notes",
        lambda *_args, **_kwargs: selection,
    )
    monkeypatch.setattr(
        rebuild_memory_index,
        "read_bounded_note",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Markdown index reread a selected path")
        ),
    )

    assert rebuild_memory_index.main() == 0
    rendered = rebuild_memory_index.out.read_text(encoding="utf-8")

    assert original_summary in rendered
    assert "MUTATED CONTENT MUST NOT BE RENDERED" not in rendered
    assert (
        f"llm-wiki-active-generation:v1:"
        f"{selection.generation.canonical_sha256}" in rendered
    )


def test_markdown_index_old_generation_cannot_overwrite_new_generation(
    tmp_path,
    monkeypatch,
):
    rebuild_memory_index, _search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(notes / "a.md", title="Generation A", body="generation a")
    old_at_publish = threading.Event()
    release_old = threading.Event()
    new_at_publish = threading.Event()
    new_finished = threading.Event()
    real_atomic_write = rebuild_memory_index.atomic_write
    outcomes: dict[str, object] = {}

    def controlled_atomic_write(path, content):
        name = threading.current_thread().name
        if name == "old-index-rebuild":
            old_at_publish.set()
            if not release_old.wait(5):
                raise TimeoutError("old index rebuild was not released")
        elif name == "new-index-rebuild":
            new_at_publish.set()
        return real_atomic_write(path, content)

    def rebuild(name: str):
        try:
            outcomes[name] = rebuild_memory_index.main()
        except BaseException as exc:  # noqa: BLE001 - surfaced by assertions
            outcomes[name] = exc
        finally:
            if name == "new":
                new_finished.set()

    monkeypatch.setattr(rebuild_memory_index, "atomic_write", controlled_atomic_write)
    old = threading.Thread(target=rebuild, args=("old",), name="old-index-rebuild")
    old.start()
    assert old_at_publish.wait(5)

    _retrieval_page(notes / "b.md", title="Generation B", body="generation b")
    new = threading.Thread(target=rebuild, args=("new",), name="new-index-rebuild")
    new.start()
    new_published_before_old_released = new_at_publish.wait(0.5)
    if new_published_before_old_released:
        assert new_finished.wait(5)
    release_old.set()
    old.join(5)
    new.join(5)

    assert new_published_before_old_released is False
    assert not old.is_alive()
    assert not new.is_alive()
    assert outcomes == {"old": 0, "new": 0}
    rendered = rebuild_memory_index.out.read_text(encoding="utf-8")
    assert "[[knowledge/notes/a]]" in rendered
    assert "[[knowledge/notes/b]]" in rendered


def test_graph_cache_is_keyed_to_selection_generation_and_uses_snapshot_content(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, _search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    import graph_neighbors
    import vault_editorial

    source = _retrieval_page(
        notes / "source.md",
        title="Graph Source",
        body="Original snapshot links to [[target]].",
    )
    target = _retrieval_page(
        notes / "target.md",
        title="Graph Target",
    )
    monkeypatch.setattr(graph_neighbors, "ROOT", tmp_path)
    monkeypatch.setattr(graph_neighbors, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(
        graph_neighbors,
        "GRAPH_CACHE",
        tmp_path / ".state" / "cache" / "link-graph.json",
        raising=False,
    )
    graph_neighbors._link_graph_cache = None
    first = vault_editorial.select_active_notes(notes, root=tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8").replace("[[target]]", "no-target!"),
        encoding="utf-8",
    )

    first_graph = graph_neighbors.get_link_graph(first)
    second = vault_editorial.select_active_notes(notes, root=tmp_path)
    second_graph = graph_neighbors.get_link_graph(second)
    source_relative = source.relative_to(tmp_path).as_posix()
    target_relative = target.relative_to(tmp_path).as_posix()

    assert first_graph[source_relative] == [target_relative]
    assert source_relative not in second_graph
    cached = json.loads(graph_neighbors.GRAPH_CACHE.read_text(encoding="utf-8"))
    assert cached["version"] == 1
    assert cached["canonical_sha256"] == second.generation.canonical_sha256

    from dataclasses import replace

    next_version = replace(
        second,
        generation=replace(second.generation, version=2),
    )
    monkeypatch.setattr(
        graph_neighbors,
        "_build_link_graph",
        lambda _selection: {source_relative: []},
    )

    assert graph_neighbors.get_link_graph(next_version) == {source_relative: []}


def test_search_and_markdown_index_share_canonical_active_set(tmp_path, monkeypatch):
    rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    flat = _retrieval_page(notes / "duplicate.md", title="Duplicate")
    _retrieval_page(notes / "patterns" / "duplicate.md", title="Duplicate")
    typed = _retrieval_page(notes / "patterns" / "typed-only.md", title="Typed Only")
    _retrieval_page(notes / "context.md", title="Generated Context")
    _retrieval_page(notes / "ArChIvE" / "archived.md", title="Archived")
    _retrieval_page(
        notes / "status-archived.md", title="Status Archived", status="archived"
    )
    invalid_project = _retrieval_page(
        notes / "invalid-project.md", title="Invalid Project", project="valid-project"
    )
    invalid_project.write_text(
        invalid_project.read_text(encoding="utf-8").replace(
            "project: valid-project", 'project: "\\qinvalid"'
        ),
        encoding="utf-8",
    )

    search_paths = set(search_memory._collect_pages())
    buckets = rebuild_memory_index.collect_pages()
    index_paths = {page for pages in buckets.values() for page in pages}

    assert search_paths == index_paths == {flat, typed}


def test_manifest_invalidates_when_canonical_duplicate_winner_changes(
    tmp_path, monkeypatch
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    flat = _retrieval_page(notes / "winner.md", title="Winner")
    typed = _retrieval_page(notes / "patterns" / "winner.md", title="Winner")
    first = search_memory._collect_note_selection()
    search_memory._build_index(first)
    assert first.paths == (flat,)
    assert search_memory._needs_rebuild(first) is False

    flat.write_text(
        flat.read_text(encoding="utf-8").replace(
            "type: pattern", "type: pattern\nstatus: archived"
        ),
        encoding="utf-8",
    )
    second = search_memory._collect_note_selection()

    assert second.paths == (typed,)
    assert search_memory._needs_rebuild(second) is True


def _make_stale_search_index(search_memory, notes: Path) -> dict[str, Path]:
    pages = {
        "live": _retrieval_page(
            notes / "live.md",
            title="Live Result",
            body="production stale filtering needle",
        ),
        "missing": _retrieval_page(
            notes / "missing.md",
            title="Missing Result",
            body="production stale filtering needle",
        ),
        "archived": _retrieval_page(
            notes / "archived.md",
            title="Archived Result",
            body="production stale filtering needle",
        ),
        "superseded": _retrieval_page(
            notes / "superseded.md",
            title="Superseded Result",
            body="production stale filtering needle",
        ),
    }
    search_memory._build_index(search_memory._collect_note_selection())
    pages["missing"].unlink()
    for status in ("archived", "superseded"):
        page = pages[status]
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "type: pattern",
                f"type: pattern\nstatus: {status}",
            ),
            encoding="utf-8",
        )
    return pages


def test_search_filters_missing_archived_and_superseded_stale_fts_rows(
    tmp_path, monkeypatch
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    pages = _make_stale_search_index(search_memory, notes)
    monkeypatch.setattr(search_memory, "_needs_rebuild", lambda _pages: False)

    results = search_memory.search("production stale filtering needle")

    assert [result["path"] for result in results] == [
        pages["live"].relative_to(tmp_path).as_posix()
    ]


def test_search_filters_allowed_paths_in_sql_before_result_limit(
    tmp_path, monkeypatch
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    live = _retrieval_page(
        notes / "z-live.md",
        title="Available Live Result",
        body=("unrelated filler " * 1_000) + "bounded canonical needle",
    )
    search_memory._build_index(search_memory._collect_note_selection())
    with sqlite3.connect(search_memory.INDEX_FILE) as connection:
        connection.executemany(
            "INSERT INTO pages (path, title, summary, body, project, timestamp) "
            "VALUES (?, ?, '', '', '', '')",
            [
                (
                    f"knowledge/notes/a-stale-{index:02d}.md",
                    "bounded canonical needle",
                )
                for index in range(12)
            ],
        )
        top_paths = [
            row[0]
            for row in connection.execute(
                "SELECT path FROM pages WHERE pages MATCH ? "
                "ORDER BY bm25(pages) LIMIT 3",
                ('"bounded" "canonical" "needle"',),
            ).fetchall()
        ]
    assert top_paths == [
        f"knowledge/notes/a-stale-{index:02d}.md" for index in range(3)
    ]
    monkeypatch.setattr(search_memory, "_needs_rebuild", lambda _pages: False)

    results = search_memory.search("bounded canonical needle", limit=1)

    assert [result["path"] for result in results] == [
        live.relative_to(tmp_path).as_posix()
    ]


def test_search_filters_stale_vector_and_graph_hits_in_production_flow(
    tmp_path, monkeypatch
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    import graph_neighbors

    pages = _make_stale_search_index(search_memory, notes)
    monkeypatch.setattr(search_memory, "_needs_rebuild", lambda _pages: False)
    monkeypatch.setattr(search_memory, "_have_sentence_transformers", lambda: True)

    def result(path: Path, score: float) -> dict:
        return {
            "path": path.relative_to(tmp_path).as_posix(),
            "title": path.stem,
            "summary": "",
            "score": score,
            "project": "",
            "timestamp": "",
        }

    monkeypatch.setattr(
        search_memory,
        "_vector_search",
        lambda *_args, **_kwargs: [
            result(pages["missing"], 4.0),
            result(pages["archived"], 3.0),
            result(pages["superseded"], 2.0),
            result(pages["live"], 1.0),
        ],
    )
    monkeypatch.setattr(
        graph_neighbors,
        "boost_graph_neighbors",
        lambda *_args, **_kwargs: [
            {
                "path": page.relative_to(tmp_path).as_posix(),
                "graph_boost": 100.0,
            }
            for page in (
                pages["missing"],
                pages["archived"],
                pages["superseded"],
                pages["live"],
            )
        ],
    )

    results = search_memory.search(
        "production stale filtering needle",
        semantic=True,
    )

    assert [result["path"] for result in results] == [
        pages["live"].relative_to(tmp_path).as_posix()
    ]


def test_fusion_cannot_reintroduce_shadow_archive_or_graph_only_paths():
    import search_memory

    live = {
        "path": "knowledge/notes/live.md",
        "title": "Live",
        "summary": "",
        "score": 3.0,
        "project": "",
        "timestamp": "",
    }
    shadow = {**live, "path": "knowledge/notes/patterns/live.md"}
    archived = {"path": "knowledge/notes/archive/old.md", "graph_boost": 100.0}

    results = search_memory._rrf_fuse_triple(
        [live],
        [shadow],
        [archived],
        allowed_paths={live["path"]},
    )

    assert [result["path"] for result in results] == [live["path"]]


def test_relevance_stays_primary_then_authority_then_confidence_without_rounding():
    import search_memory

    results = [
        {"path": "ai-high.md", "score": 1.0001, "_authority_rank": 2, "_confidence_rank": 3},
        {"path": "web-low.md", "score": 1.0001, "_authority_rank": 3, "_confidence_rank": 1},
        {"path": "web-high.md", "score": 1.0001, "_authority_rank": 3, "_confidence_rank": 3},
        {"path": "user-less-relevant.md", "score": 1.0, "_authority_rank": 4, "_confidence_rank": 3},
        {"path": "precise.md", "score": 1.00009, "_authority_rank": 4, "_confidence_rank": 3},
    ]

    ranked = search_memory._sort_search_results(results, score_field="score")

    assert [item["path"] for item in ranked] == [
        "web-high.md",
        "web-low.md",
        "ai-high.md",
        "precise.md",
        "user-less-relevant.md",
    ]


def test_bm25_relevance_ten_beats_user_authority_relevance_nine(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    inferred = _retrieval_page(
        notes / "inferred.md",
        title="Inferred Result",
        authority="inferred",
        confidence="high",
    )
    user = _retrieval_page(
        notes / "user.md",
        title="User Result",
        authority="user",
        confidence="low",
    )
    selection = search_memory._collect_note_selection()
    inferred_path = inferred.relative_to(tmp_path).as_posix()
    user_path = user.relative_to(tmp_path).as_posix()
    monkeypatch.setattr(search_memory, "_needs_rebuild", lambda _selection: False)
    monkeypatch.setattr(
        search_memory,
        "_query_fts",
        lambda *_args, **_kwargs: [
            (inferred_path, "Inferred Result", "", "", "", -10.0),
            (user_path, "User Result", "", "", "", -9.0),
        ],
    )
    monkeypatch.setattr(
        search_memory,
        "_collect_note_selection",
        lambda _scope="all": selection,
    )

    results = search_memory.search("unrelated query", as_of="2026-08-03")

    assert [result["path"] for result in results[:2]] == [inferred_path, user_path]
    assert results[0]["score"] > results[1]["score"]


def test_vector_relevance_ten_beats_user_authority_relevance_nine(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    _retrieval_page(
        notes / "inferred-vector.md",
        title="Inferred Vector",
        authority="inferred",
        confidence="high",
    )
    _retrieval_page(
        notes / "user-vector.md",
        title="User Vector",
        authority="user",
        confidence="low",
    )
    selection = search_memory._collect_note_selection()
    paths = [note.relative_path for note in selection.notes]
    notes_by_path = {note.relative_path: note for note in selection.notes}
    monkeypatch.setattr(
        search_memory,
        "_load_or_build_vectors",
        lambda _selection: {
            "paths": paths,
            "titles": ["Inferred Vector", "User Vector"],
            "summaries": ["", ""],
            "projects": ["", ""],
            "timestamps": ["", ""],
            "vectors": [[0.0], [0.0]],
        },
    )
    monkeypatch.setattr(search_memory, "_embed_texts", lambda _texts: [[0.0]])
    monkeypatch.setattr(
        search_memory,
        "_cosine_similarity",
        lambda _query, _documents: [10.0, 9.0],
    )

    results = search_memory._vector_search(
        "unrelated query",
        selection,
        2,
        as_of="2026-08-03",
        notes_by_path=notes_by_path,
        allowed_paths=set(paths),
    )

    assert results is not None
    assert [result["path"] for result in results] == paths
    assert results[0]["score"] > results[1]["score"]


def test_exact_relevance_tie_uses_authority_then_confidence_then_path():
    import search_memory

    results = [
        {"path": "inferred-high.md", "score": 10.0, "_authority_rank": 1, "_confidence_rank": 3},
        {"path": "user-low.md", "score": 10.0, "_authority_rank": 4, "_confidence_rank": 1},
        {"path": "user-high-z.md", "score": 10.0, "_authority_rank": 4, "_confidence_rank": 3},
        {"path": "user-high-a.md", "score": 10.0, "_authority_rank": 4, "_confidence_rank": 3},
    ]

    ranked = search_memory._sort_search_results(results, score_field="score")

    assert [result["path"] for result in ranked] == [
        "user-high-a.md",
        "user-high-z.md",
        "user-low.md",
        "inferred-high.md",
    ]


def test_index_build_transform_failure_preserves_existing_database(tmp_path, monkeypatch):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch, tmp_path
    )
    _retrieval_page(notes / "source.md", title="Source")
    search_memory.INDEX_DIR.mkdir(parents=True)
    search_memory.INDEX_FILE.write_bytes(b"existing database sentinel")
    selection = search_memory._collect_note_selection()
    monkeypatch.setattr(
        search_memory,
        "_extract_title_and_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated snapshot transformation failure")
        ),
    )

    with pytest.raises(OSError, match="snapshot transformation"):
        search_memory._build_index(selection)

    assert search_memory.INDEX_FILE.read_bytes() == b"existing database sentinel"
    assert not search_memory.INDEX_FILE.with_suffix(".sqlite.tmp").exists()
    assert not search_memory.INDEX_MANIFEST.exists()


def test_search_recovers_once_from_post_validation_sqlite_corruption_and_closes_all(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    page = _retrieval_page(
        notes / "recover.md",
        title="Recover Corrupt Index",
        body="sqlite recovery needle",
    )
    selection = search_memory._collect_note_selection()
    search_memory._build_index(selection)
    real_connect = search_memory.sqlite3.connect
    real_build = search_memory._build_index
    connections = []
    rebuilds: list[str] = []

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.closed = True
            return self.connection.close()

    def tracked_connect(*args, **kwargs):
        tracked = TrackedConnection(real_connect(*args, **kwargs))
        connections.append(tracked)
        return tracked

    def corrupt_after_validation(_selection):
        search_memory.INDEX_FILE.write_bytes(b"not a sqlite database")
        return False

    def counted_build(current):
        rebuilds.append(current.generation.canonical_sha256)
        return real_build(current)

    monkeypatch.setattr(search_memory.sqlite3, "connect", tracked_connect)
    monkeypatch.setattr(search_memory, "_needs_rebuild", corrupt_after_validation)
    monkeypatch.setattr(search_memory, "_build_index", counted_build)

    results = search_memory.search("sqlite recovery needle")

    assert [result["path"] for result in results] == [
        page.relative_to(tmp_path).as_posix()
    ]
    assert rebuilds == [selection.generation.canonical_sha256]
    assert len(connections) == 3
    assert all(connection.closed for connection in connections)


def test_query_fts_rebuilds_valid_old_generation_swapped_after_rebuild_check(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    page = _retrieval_page(
        notes / "generation-swap.md",
        title="Generation Swap",
        body="oldgenerationswapneedle",
    )
    old_selection = search_memory._collect_note_selection()
    search_memory._build_index(old_selection)
    old_index = search_memory.INDEX_FILE.with_name("old-generation.sqlite")
    old_index.write_bytes(search_memory.INDEX_FILE.read_bytes())

    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "oldgenerationswapneedle",
            "newgenerationswapneedle",
        ),
        encoding="utf-8",
    )
    current_selection = search_memory._collect_note_selection()
    search_memory._build_index(current_selection)
    real_build = search_memory._build_index
    rebuilds: list[str] = []

    def swap_after_rebuild_check(selection):
        assert selection.generation == current_selection.generation
        os.replace(old_index, search_memory.INDEX_FILE)
        return False

    def counted_build(selection):
        rebuilds.append(selection.generation.canonical_sha256)
        return real_build(selection)

    monkeypatch.setattr(search_memory, "_needs_rebuild", swap_after_rebuild_check)
    monkeypatch.setattr(search_memory, "_build_index", counted_build)

    results = search_memory.search("newgenerationswapneedle")

    assert [result["path"] for result in results] == [
        page.relative_to(tmp_path).as_posix()
    ]
    assert rebuilds == [current_selection.generation.canonical_sha256]


def test_filtered_shadow_does_not_consume_vector_or_graph_rank():
    import search_memory

    live = {
        "path": "knowledge/notes/live.md",
        "title": "Live",
        "summary": "",
        "score": 1.0,
        "project": "",
        "timestamp": "",
    }
    shadow = {**live, "path": "knowledge/notes/patterns/live.md"}
    graph_shadow = {"path": shadow["path"], "graph_boost": 1.0}
    graph_live = {"path": live["path"], "graph_boost": 1.0}
    allowed = {live["path"]}

    with_shadows = search_memory._rrf_fuse_triple(
        [],
        [shadow, live],
        [graph_shadow, graph_live],
        allowed_paths=allowed,
    )
    without_shadows = search_memory._rrf_fuse_triple(
        [],
        [live],
        [graph_live],
        allowed_paths=allowed,
    )

    assert with_shadows[0]["fused_score"] == without_shadows[0]["fused_score"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("as_of", "2026-13-40"),
        ("as_of", "20260803"),
        ("as_of", "2026-8-03"),
        ("since", "not-a-date"),
    ),
)
def test_search_rejects_noncanonical_or_impossible_iso_dates_before_collection(
    monkeypatch,
    field,
    value,
):
    import search_memory

    monkeypatch.setattr(
        search_memory,
        "_collect_note_selection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid date reached retrieval")
        ),
    )

    with pytest.raises(ValueError, match="valid YYYY-MM-DD"):
        search_memory.search("date validation", **{field: value})


def test_as_of_rejects_invalid_snapshot_temporal_metadata(
    tmp_path,
    monkeypatch,
):
    _rebuild_memory_index, search_memory, notes = _configure_retrieval_modules(
        monkeypatch,
        tmp_path,
    )
    valid = _retrieval_page(
        notes / "valid.md",
        title="Authority Weighted Result",
        authority="user",
        body="authority weighting needle",
    )
    invalid = _retrieval_page(
        notes / "invalid.md",
        title="Malformed Validity Result",
        authority="web",
        body="authority weighting needle",
    )
    invalid.write_text(
        invalid.read_text(encoding="utf-8").replace(
            "type: pattern",
            "type: pattern\nvalid_to: 2026-99-99",
        ),
        encoding="utf-8",
    )
    invalid_quoted = _retrieval_page(
        notes / "invalid-quoted.md",
        title="Malformed Quoted Validity Result",
        authority="web",
        body="authority weighting needle",
    )
    invalid_quoted.write_text(
        invalid_quoted.read_text(encoding="utf-8").replace(
            "type: pattern",
            'type: pattern\nvalid_to: "2026-08-03\'',
        ),
        encoding="utf-8",
    )
    invalid_timestamp = _retrieval_page(
        notes / "invalid-timestamp.md",
        title="Malformed Timestamp Result",
        authority="web",
        body="authority weighting needle",
    )
    invalid_timestamp.write_text(
        invalid_timestamp.read_text(encoding="utf-8").replace(
            "type: pattern",
            "type: pattern\ntimestamp: 2026-08-03junk",
        ),
        encoding="utf-8",
    )
    results = search_memory.search(
        "authority weighting needle",
        as_of="2026-08-03",
    )

    assert [result["path"] for result in results] == [
        valid.relative_to(tmp_path).as_posix()
    ]


def test_benchmark_pairs_use_only_canonical_active_selection(tmp_path, monkeypatch):
    import importlib.util

    benchmark_path = Path(__file__).resolve().parent.parent / "benchmark" / "run_benchmark.py"
    spec = importlib.util.spec_from_file_location("test_run_benchmark", benchmark_path)
    assert spec is not None and spec.loader is not None
    run_benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_benchmark)

    notes = tmp_path / "knowledge" / "notes"
    flat = _retrieval_page(
        notes / "canonical.md",
        title="Canonical Benchmark Page",
        body="canonical benchmark summary words",
    )
    _retrieval_page(
        notes / "patterns" / "duplicate.md",
        title="Canonical Benchmark Page",
        body="shadow benchmark summary words",
    )
    _retrieval_page(
        notes / "archived.md",
        title="Archived Benchmark Page",
        status="archived",
        body="archived benchmark summary words",
    )
    monkeypatch.setattr(run_benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(run_benchmark, "KNOWLEDGE", notes)

    pairs = run_benchmark._generate_qa_pairs()

    assert {pair["gold_path"] for pair in pairs} == {
        flat.relative_to(tmp_path).as_posix()
    }
