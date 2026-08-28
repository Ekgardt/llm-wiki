"""A rebuild must reuse what did not change, and prove it changed nothing.

Before this suite the answer to both halves was no. `_stored_incremental_manifest`
dropped any manifest over 64 MiB, and on this vault's corpus the manifest was
158,075,010 bytes because it carried one row per record for 349,306 records — so
none of the 33 generations on disk had one, `reused_sources` was always 0, and
`build_generation_vectors_if_available` re-encoded every chunk of every build.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# --------------------------------------------------------------------------
# Fixtures: the smallest extractor that still owns records per source.
# --------------------------------------------------------------------------


def _extraction(source_id: str, *, imports: tuple[str, ...] = ()) -> object:
    import evidence_graph_builder

    digest = f"{abs(hash(source_id)) % (10 ** 12):064d}"
    return evidence_graph_builder.SourceExtraction(
        nodes=(
            {
                "node_id": f"node:{source_id}",
                "kind": "page",
                "identity_scheme": "path",
                "identity_key": source_id,
                "metadata": {},
            },
        ),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        source_dependencies=tuple(sorted(imports)),
        invalidation_fingerprints={
            "exports": digest,
            "imports": digest,
            "signatures": digest,
            "aliases": digest,
            "project_metadata": digest,
        },
        workspace_sensitive=False,
    )


class _Extractor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, source, content, **kwargs):  # noqa: ARG002 - fixture shape
        source_id = str(source["source_id"])
        self.calls.append(source_id)
        return _extraction(source_id)


def _reuse_config():
    import evidence_graph_builder

    return evidence_graph_builder.IncrementalReuseConfig(
        extractor_version="fixture/v1",
        grammar_version="fixture/v1",
        compiler_version="fixture/v1",
        resolver_config_sha256="0" * 64,
        schema_version=evidence_graph_builder.GRAPH_SCHEMA_VERSION,
        workspace_manifest_sha256="0" * 64,
    )


def _build(catalog, generation_id, files, extractor, parent=None):
    import hashlib

    import evidence_graph_builder

    sources = []
    content = {}
    for source_id, (relative_path, body) in sorted(files.items()):
        raw = body.encode("utf-8")
        sources.append(
            {
                "source_id": source_id,
                "relative_path": relative_path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "media_type": "text/markdown",
                "language": None,
                "git_oid": None,
            }
        )
        content[source_id] = raw
    return evidence_graph_builder.build_incremental_generation(
        catalog,
        sources=sources,
        source_bytes=content,
        extractor=extractor,
        reuse_config=_reuse_config(),
        generation_id=generation_id,
        parent_generation_id=parent,
        expected_active=parent,
    )


_FILES = {
    "one": ("one.md", "# one\n"),
    "two": ("two.md", "# two\n"),
    "three": ("three.md", "# three\n"),
}


# --------------------------------------------------------------------------
# Half one: record reuse.
# --------------------------------------------------------------------------


def test_a_generation_stores_a_manifest_the_next_build_can_read(tmp_path):
    """The defect in one line: no generation on disk carried a manifest."""
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    built = _build(catalog, "gen-1", _FILES, _Extractor())

    assert (built.generation_path / "incremental-manifest.json").is_file()


def test_an_unchanged_source_is_reused_rather_than_extracted_again(tmp_path):
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _build(catalog, "gen-1", _FILES, _Extractor())
    extractor = _Extractor()

    second = _build(catalog, "gen-2", _FILES, extractor, parent="gen-1")

    assert second.reused_sources == ("one", "three", "two")
    assert second.rebuilt_sources == ()
    assert extractor.calls == []


def test_only_the_changed_source_is_extracted_again(tmp_path):
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    _build(catalog, "gen-1", _FILES, _Extractor())
    changed = {**_FILES, "two": ("two.md", "# two, edited\n")}
    extractor = _Extractor()

    second = _build(catalog, "gen-2", changed, extractor, parent="gen-1")

    assert second.rebuilt_sources == ("two",)
    assert second.reused_sources == ("one", "three")
    assert extractor.calls == ["two"]


def test_the_manifest_carries_a_bounded_sample_not_a_row_per_record(tmp_path):
    """One `record_dependencies` row per record is what made it unstorable.

    158,075,010 bytes for 349,306 records, against a 64 MiB ceiling — so the
    manifest was dropped on every pass. No reader in `scripts/` consumes those
    rows, and they are derivable from `sources`, so what is kept is a bounded
    deterministic prefix and the manifest states the true total.
    """
    import evidence_graph_builder
    from generation_catalog import GenerationCatalog

    def wide(source, content, **kwargs):  # noqa: ARG001 - fixture shape
        source_id = str(source["source_id"])
        base = _extraction(source_id)
        return evidence_graph_builder.SourceExtraction(
            **{
                **{
                    field: getattr(base, field)
                    for field in base.__dataclass_fields__
                },
                "nodes": tuple(
                    {
                        "node_id": f"node:{source_id}:{index}",
                        "kind": "page",
                        "identity_scheme": "path",
                        "identity_key": f"{source_id}:{index}",
                        "metadata": {},
                    }
                    for index in range(10)
                ),
            }
        )

    monkey = evidence_graph_builder.MAX_INLINE_RECORD_DEPENDENCY_ROWS
    evidence_graph_builder.MAX_INLINE_RECORD_DEPENDENCY_ROWS = 5
    try:
        catalog = GenerationCatalog(tmp_path / "state")
        built = _build(catalog, "gen-1", _FILES, wide)
    finally:
        evidence_graph_builder.MAX_INLINE_RECORD_DEPENDENCY_ROWS = monkey

    manifest = json.loads(
        (built.generation_path / "incremental-manifest.json").read_bytes()
    )
    assert manifest["record_dependencies_total"] == 30
    assert len(manifest["record_dependencies"]) == 5


def test_the_read_bound_is_the_size_the_sealed_manifest_declares(tmp_path):
    """Not a constant: a constant is what a real corpus outgrew.

    `generation_catalog._validate_generation` has already hashed the artifact
    against `manifest.json`, so the declared size is verified fact by the time
    the reuse path reads it.
    """
    import evidence_graph_builder
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    built = _build(catalog, "gen-1", _FILES, _Extractor())
    sealed = json.loads((built.generation_path / "manifest.json").read_bytes())
    actual = (built.generation_path / "incremental-manifest.json").stat().st_size

    assert evidence_graph_builder._declared_manifest_bytes(sealed) == actual


def test_a_manifest_the_sealed_generation_does_not_declare_is_refused(tmp_path):
    import evidence_graph_builder

    with pytest.raises(ValueError, match="does not declare"):
        evidence_graph_builder._declared_manifest_bytes(
            {"artifacts": [{"path": "evidence.sqlite3", "size": 1, "sha256": "0" * 64}]}
        )


def test_the_store_ceiling_is_no_longer_a_constant_a_corpus_can_outgrow(tmp_path):
    """The old ceiling was 64 MiB and this vault's manifest was 158 MB."""
    import evidence_graph_builder

    assert evidence_graph_builder.MAX_STORED_INCREMENTAL_MANIFEST_BYTES > 158_075_010


def test_an_incremental_graph_is_byte_identical_to_a_full_rebuild(tmp_path):
    """Correctness outranks speed, and it is compared rather than asserted."""
    import sqlite3

    from generation_catalog import GenerationCatalog

    changed = {**_FILES, "two": ("two.md", "# two, edited\n")}
    incremental_catalog = GenerationCatalog(tmp_path / "incremental")
    _build(incremental_catalog, "base", _FILES, _Extractor())
    incremental = _build(
        incremental_catalog, "next", changed, _Extractor(), parent="base"
    )
    full_catalog = GenerationCatalog(tmp_path / "full")
    full = _build(full_catalog, "full", changed, _Extractor())

    assert incremental.reused_sources == ("one", "three")

    def rows(path):
        with sqlite3.connect(path) as database:
            database.row_factory = sqlite3.Row
            return {
                table: [
                    dict(row)
                    for row in database.execute(f"SELECT * FROM {table} ORDER BY 1")
                ]
                for table in (
                    "node",
                    "occurrence",
                    "assertion",
                    "evidence",
                    "observation",
                    "dependency",
                    "source",
                )
            }

    assert rows(incremental.generation_path / "evidence.sqlite3") == rows(
        full.generation_path / "evidence.sqlite3"
    )


# --------------------------------------------------------------------------
# Half two: vector reuse.
# --------------------------------------------------------------------------


class _CountingEmbedder:
    """A deterministic stand-in: the vector is a function of the text alone.

    That is the property the real model has only up to float32 re-batching
    noise, so the reuse rule is tested here against a model that has it
    exactly, and the real model's departure from it is measured separately in
    `docs/research/2026-08-28-what-a-rebuild-may-reuse.md`.
    """

    def __init__(self, dimensions: int = 4) -> None:
        self.dimensions = dimensions
        self.encoded: list[str] = []

    def __call__(self, texts):
        import hashlib

        rows = []
        for text in texts:
            self.encoded.append(text)
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            rows.append([digest[index] / 255.0 for index in range(self.dimensions)])
        return rows


def _snapshot_from(tmp_path: Path, pages: dict[str, str]):
    from corpus_snapshot import collect_corpus

    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    for name, body in pages.items():
        (notes / name).write_text(body, encoding="utf-8")
    return collect_corpus(tmp_path, code_roots=(), max_files=100)


_PAGE = """---
type: concept
status: active
---

# {title}

One-sentence summary: {title} exists so a chunk exists.

{body}
"""


def test_an_unchanged_chunk_is_not_embedded_again(tmp_path):
    import search_memory

    pages = {
        "a.md": _PAGE.format(title="Alpha", body="alpha body"),
        "b.md": _PAGE.format(title="Beta", body="beta body"),
    }
    snapshot = _snapshot_from(tmp_path / "vault", pages)
    first_dir = tmp_path / "gen-1"
    first_dir.mkdir()
    embedder = _CountingEmbedder()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        first_dir,
        embedder=embedder,
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )
    assert embedder.encoded

    second_dir = tmp_path / "gen-2"
    second_dir.mkdir()
    reuse_embedder = _CountingEmbedder()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        second_dir,
        embedder=reuse_embedder,
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
        reuse_from=first_dir,
    )

    assert reuse_embedder.encoded == []


def test_a_reused_matrix_is_byte_identical_to_a_full_build(tmp_path):
    import numpy as np
    import search_memory

    pages = {
        "a.md": _PAGE.format(title="Alpha", body="alpha body"),
        "b.md": _PAGE.format(title="Beta", body="beta body"),
    }
    snapshot = _snapshot_from(tmp_path / "vault", pages)
    parent = tmp_path / "gen-1"
    parent.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        parent,
        embedder=_CountingEmbedder(),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )

    changed = _snapshot_from(
        tmp_path / "vault", {**pages, "b.md": _PAGE.format(title="Beta", body="new")}
    )
    incremental_dir = tmp_path / "gen-2"
    incremental_dir.mkdir()
    search_memory.build_generation_numpy_vectors(
        changed,
        incremental_dir,
        embedder=_CountingEmbedder(),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
        reuse_from=parent,
    )
    full_dir = tmp_path / "gen-full"
    full_dir.mkdir()
    search_memory.build_generation_numpy_vectors(
        changed,
        full_dir,
        embedder=_CountingEmbedder(),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )

    assert (incremental_dir / "vectors.npy").read_bytes() == (
        full_dir / "vectors.npy"
    ).read_bytes()
    assert (incremental_dir / "vectors.json").read_bytes() == (
        full_dir / "vectors.json"
    ).read_bytes()
    assert np.array_equal(
        np.load(incremental_dir / "vectors.npy"), np.load(full_dir / "vectors.npy")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_id", "fixture/other"),
        ("model_revision", "rev-2"),
    ],
)
def test_another_model_invalidates_every_cached_vector(tmp_path, field, value):
    """Model identity is the namespace; a different one shares no rows."""
    import search_memory

    pages = {"a.md": _PAGE.format(title="Alpha", body="alpha body")}
    snapshot = _snapshot_from(tmp_path / "vault", pages)
    parent = tmp_path / "gen-1"
    parent.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        parent,
        embedder=_CountingEmbedder(),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )

    second = tmp_path / "gen-2"
    second.mkdir()
    embedder = _CountingEmbedder()
    arguments = {
        "model_id": "fixture/model",
        "model_revision": "rev-1",
        field: value,
    }
    search_memory.build_generation_numpy_vectors(
        snapshot,
        second,
        embedder=embedder,
        dimensions=4,
        reuse_from=parent,
        **arguments,
    )

    assert len(embedder.encoded) == len(snapshot.chunks)


def test_a_different_dimension_invalidates_every_cached_vector(tmp_path):
    import search_memory

    pages = {"a.md": _PAGE.format(title="Alpha", body="alpha body")}
    snapshot = _snapshot_from(tmp_path / "vault", pages)
    parent = tmp_path / "gen-1"
    parent.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        parent,
        embedder=_CountingEmbedder(4),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )

    second = tmp_path / "gen-2"
    second.mkdir()
    embedder = _CountingEmbedder(8)
    search_memory.build_generation_numpy_vectors(
        snapshot,
        second,
        embedder=embedder,
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=8,
        reuse_from=parent,
    )

    assert len(embedder.encoded) == len(snapshot.chunks)


def test_a_corrupt_parent_matrix_costs_reuse_and_never_correctness(tmp_path):
    import search_memory

    pages = {"a.md": _PAGE.format(title="Alpha", body="alpha body")}
    snapshot = _snapshot_from(tmp_path / "vault", pages)
    parent = tmp_path / "gen-1"
    parent.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        parent,
        embedder=_CountingEmbedder(),
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
    )
    (parent / "vectors.npy").write_bytes(b"not a numpy file")

    second = tmp_path / "gen-2"
    second.mkdir()
    embedder = _CountingEmbedder()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        second,
        embedder=embedder,
        model_id="fixture/model",
        model_revision="rev-1",
        dimensions=4,
        reuse_from=parent,
    )

    assert len(embedder.encoded) == len(snapshot.chunks)
