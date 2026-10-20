"""Digest-only consumers release bodies while index planning preserves byte provenance."""
import tracemalloc

import claim_tree_manifest as claims
import pytest
import rebuild_memory_index as index


def _page(root, name, body):
    path = root / "knowledge/notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _peak(call):
    call()
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("reader", [claims.snapshot_claim_tree, claims.snapshot_guardrail_sources])
def test_manifest_only_capture_releases_previous_bodies(tmp_path, reader):
    body = b"---\ntype: concept\n---\n# Page\n" + b"x" * (1024 * 1024)
    for number in range(16):
        _page(tmp_path, f"{number}.md", body)
    # The previous implementation retains all sixteen bodies even when returning
    # only digests. Allow transient copies, but not half the source payloads.
    assert _peak(lambda: reader(tmp_path)) < 8 * len(body)


@pytest.mark.parametrize("plain,with_content", [
    (claims.snapshot_claim_tree, claims.snapshot_claim_tree_with_content),
    (claims.snapshot_guardrail_sources, claims.snapshot_guardrail_sources_with_content),
])
def test_content_bearing_consumers_keep_exact_bytes_and_identical_manifests(tmp_path, plain, with_content):
    body = b"---\ntype: concept\n---\n# Original\n"
    _page(tmp_path, "one.md", body)
    manifest, contents = with_content(tmp_path)
    assert plain(tmp_path) == manifest
    assert contents == {"knowledge/notes/one.md": body}


def test_standalone_plan_does_not_materialize_the_whole_tree(tmp_path, monkeypatch):
    body = b"---\ntype: concept\n---\n# Page\nOne-sentence summary: Kept.\n" + b"x" * (1024 * 1024)
    for number in range(16):
        _page(tmp_path, f"{number}.md", body)
    monkeypatch.setattr(index, "ROOT", tmp_path)
    monkeypatch.setattr(index, "out", tmp_path / "knowledge/index.md")
    assert _peak(lambda: index._rebuild_plan("")) < 8 * len(body)
    _operation, content, preconditions = index._rebuild_plan("")
    manifest, bodies = claims.snapshot_claim_tree_with_content(tmp_path)
    assert content == index.build_index_bytes(tmp_path, base=bodies)
    assert preconditions["claim_tree_manifest"] == manifest


def test_changed_body_between_manifest_and_render_is_refused(tmp_path, monkeypatch):
    page = _page(tmp_path, "one.md", b"---\ntype: concept\n---\n# Original\n")
    original = index.snapshot_claim_tree

    def change_after_snapshot(root):
        manifest = original(root)
        page.write_bytes(b"---\ntype: concept\n---\n# Changed\n")
        return manifest

    monkeypatch.setattr(index, "ROOT", tmp_path)
    monkeypatch.setattr(index, "out", tmp_path / "knowledge/index.md")
    monkeypatch.setattr(index, "snapshot_claim_tree", change_after_snapshot)
    with pytest.raises(claims.ClaimTreeChanged, match="differs from claim snapshot"):
        index._rebuild_plan("")
    assert not index.out.exists()


def test_manifest_mapping_refuses_an_unlisted_path_before_reading(tmp_path, monkeypatch):
    manifest = claims.snapshot_claim_tree(tmp_path)
    pages = index._ManifestIndexPages(tmp_path, manifest)

    def forbidden(*args, **kwargs):
        pytest.fail("an unlisted path must never be read")

    monkeypatch.setattr(index, "read_stable_bytes", forbidden)
    with pytest.raises(KeyError):
        pages["knowledge/notes/unlisted.md"]
