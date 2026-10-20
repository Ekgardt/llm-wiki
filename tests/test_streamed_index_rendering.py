"""Index rendering keeps navigation metadata, not the complete corpus bodies."""
import tracemalloc

import pytest
import rebuild_memory_index as index


def _page(title):
    return f"---\ntype: concept\n---\n# {title}\nOne-sentence summary: {title}.\n".encode()


def test_renderer_does_not_retain_all_source_payloads(tmp_path):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    body = _page("Kept") + b"x" * (1024 * 1024)
    for number in range(16):
        (notes / f"{number}.md").write_bytes(body)
    expected = index.build_index_bytes(tmp_path)
    tracemalloc.start()
    try:
        actual = index.build_index_bytes(tmp_path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected
    # The old renderer retains all sixteen bodies; a half-corpus allowance
    # distinguishes that regression while allowing several transient bodies.
    assert peak < 8 * len(body)


def test_overlay_matches_a_materialized_final_tree_without_mutating_base(tmp_path):
    base = {
        "knowledge/notes/z.md": _page("Old"),
        "knowledge/notes/remove.md": _page("Removed"),
        "knowledge/notes/a.md": _page("Original"),
    }
    pending = {
        "knowledge/notes/z.md": _page("Replacement"),
        "knowledge/notes/remove.md": None,
        "knowledge/notes/new.md": _page("New"),
        "knowledge/notes/absent.md": None,
    }
    original_base, original_pending = dict(base), dict(pending)
    final = {"knowledge/notes/a.md": _page("Original"),
             "knowledge/notes/z.md": _page("Replacement"),
             "knowledge/notes/new.md": _page("New")}
    assert index.build_index_bytes(tmp_path, pending, base=base) == index.build_index_bytes(tmp_path, base=final)
    assert base == original_base
    assert pending == original_pending


@pytest.mark.parametrize("key,value", [("max_pages", 1), ("max_total_bytes", 1)])
def test_explicit_quota_applies_before_pending_deletions(tmp_path, key, value):
    (tmp_path / "llm-wiki.toml").write_text(f"[index]\n{key} = {value}\n")
    base = {"knowledge/notes/a.md": _page("A"), "knowledge/notes/b.md": _page("B")}
    pending = dict.fromkeys(base)
    with pytest.raises(index.IndexCapacityError):
        index.build_index_bytes(tmp_path, pending, base=base)


def test_explicit_quota_also_applies_to_pending_additions(tmp_path):
    (tmp_path / "llm-wiki.toml").write_text("[index]\nmax_pages = 1\n")
    with pytest.raises(index.IndexCapacityError, match="page count"):
        index.build_index_bytes(tmp_path, {"knowledge/notes/b.md": _page("B")},
                                base={"knowledge/notes/a.md": _page("A")})


def test_each_disk_source_is_read_once_including_private_sources(tmp_path, monkeypatch):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    (notes / "visible.md").write_bytes(_page("Visible"))
    (notes / "private.md").write_bytes(_page("Private"))
    (tmp_path / ".gitignore").write_text("knowledge/notes/*\n!knowledge/notes/visible.md\n")
    original = index.read_stable_bytes
    reads = []

    def observe(path, *args, **kwargs):
        reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(index, "read_stable_bytes", observe)
    rendered = index.build_index_bytes(tmp_path)
    assert sorted(reads) == ["private.md", "visible.md"]
    assert b"visible" in rendered
    assert b"private" not in rendered


@pytest.mark.parametrize("count,size", [(2001, 0), (33, 1024 * 1024)])
def test_default_index_quotas_do_not_reintroduce_old_cutoffs(tmp_path, count, size):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    body = _page("Kept") + b"x" * size
    for number in range(count):
        (notes / f"{number}.md").write_bytes(body)
    rendered = index.build_index_bytes(tmp_path)
    assert rendered.count(b"- [[knowledge/notes/") == count


def test_explicit_zero_index_quotas_are_reported_as_disabled(tmp_path):
    import doctor

    (tmp_path / "llm-wiki.toml").write_text("[index]\nmax_pages = 0\nmax_total_bytes = 0\n")
    rendered = index.build_index_bytes(tmp_path, base={"knowledge/notes/a.md": _page("A")})
    assert b"knowledge/notes/a" in rendered
    details = doctor._settings_check(tmp_path)["details"]
    assert "index.max_pages" not in details["near_ceiling"]
    assert "index.max_total_bytes" not in details["near_ceiling"]
    assert details["overridden"]["index.max_pages"] == {"value": 0, "source": "llm-wiki.toml"}
