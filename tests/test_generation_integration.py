"""Cross-consumer integration tests for one immutable corpus generation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_tiers  # noqa: E402
import contextual_retrieval  # noqa: E402
import rebuild_lance_index  # noqa: E402
import search_memory  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402
from generation_catalog import GenerationCatalog  # noqa: E402
from reliable_memory import canonical_json_bytes  # noqa: E402

MODEL_ID = "deterministic/integration"
MODEL_REVISION = "revision-1"
VECTOR_DIMENSIONS = 3


class _DeterministicEmbedder:
    def encode(self, texts, **_kwargs):
        return [
            [float(index), float(len(text)), float(sum(text.encode("utf-8")) % 997)]
            for index, text in enumerate(texts)
        ]


class _DeterministicLanceAdapter:
    def write(self, output_dir: Path, rows: list[dict], *, vector_dimension: int):
        assert vector_dimension == VECTOR_DIMENSIONS
        output_dir.mkdir(parents=True)
        (output_dir / "rows.json").write_bytes(
            json.dumps(
                rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


def _page(title: str, body: str, **metadata: str) -> str:
    fields = "\n".join(f"{key}: {value}" for key, value in metadata.items())
    return f"---\n{fields}\n---\n# {title}\n{body}\n"


def _write_snapshot_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    first = vault / "knowledge/notes/concept/same.md"
    second = vault / "knowledge/notes/pattern/same.md"
    superseded = vault / "knowledge/notes/old.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        _page(
            "First",
            "First evidence.\n## Detail\nFirst detail.",
            type="concept",
            project="demo",
            source_authority="user",
            confidence="high",
        ),
        encoding="utf-8",
        newline="",
    )
    second.write_text(
        _page("Second", "Second evidence.", type="pattern"),
        encoding="utf-8",
        newline="",
    )
    superseded.write_text(
        _page("Old", "Excluded evidence.", type="concept", status="superseded"),
        encoding="utf-8",
        newline="",
    )
    return vault, first


def _build_generation(snapshot, catalog: GenerationCatalog, generation_id: str):
    generation = catalog.generations_path / generation_id
    generation.mkdir()
    descriptors = [search_memory.build_generation_fts(snapshot, generation)]
    descriptors.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=_DeterministicEmbedder(),
            model_id=MODEL_ID,
            model_revision=MODEL_REVISION,
            dimensions=VECTOR_DIMENSIONS,
        )
    )
    descriptors.extend(contextual_retrieval.build_snapshot_contexts(snapshot, generation))
    descriptors.extend(build_tiers.build_snapshot_tiers(snapshot, generation))
    descriptors.extend(
        rebuild_lance_index.build_lance_generation(
            snapshot,
            generation,
            generation_root=catalog.generations_path,
            embedder=_DeterministicEmbedder(),
            embedding_model_id=MODEL_ID,
            embedding_model_revision=MODEL_REVISION,
            embedding_dimensions=VECTOR_DIMENSIONS,
            lance_adapter=_DeterministicLanceAdapter(),
        )
    )
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": MODEL_ID,
        "embedding_model_revision": MODEL_REVISION,
        "vector_dimensions": VECTOR_DIMENSIONS,
        "graph_schema_version": None,
        "graph_extractor_version": None,
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": sorted(descriptors, key=lambda item: item["path"]),
        "vector_state": "complete",
    }
    (generation / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return generation, manifest


def _source_membership(snapshot) -> list[tuple[str, str, str]]:
    return [
        (
            source.record.logical_id,
            source.record.relative_path,
            source.record.sha256,
        )
        for source in snapshot.sources
    ]


@pytest.fixture
def numpy_module():
    return pytest.importorskip("numpy")


@dataclass(frozen=True)
class _Published:
    """One generation, built once, for the consumers that all read it."""

    vault: Path
    snapshot: object
    catalog: object
    generation: Path
    manifest: dict
    registered: dict
    active: dict


@pytest.fixture
def published(tmp_path: Path) -> _Published:
    vault, _source = _write_snapshot_vault(tmp_path)
    snapshot = collect_corpus(vault)
    catalog = GenerationCatalog(tmp_path / "state")
    generation, manifest = _build_generation(snapshot, catalog, "generation-1")
    registered = catalog.register("generation-1")
    assert catalog.activate("generation-1", expected_active=None) is True
    return _Published(
        vault, snapshot, catalog, generation, manifest, registered, catalog.get_active()
    )


def _chunk_sources(snapshot) -> list[tuple]:
    return [
        (chunk.source_id, chunk.source_path, chunk.source_sha256)
        for chunk in snapshot.chunks
    ]


def test_the_snapshot_selects_the_current_pages_and_not_the_retired_one(published):
    membership = _source_membership(published.snapshot)

    assert [source[1] for source in membership] == [
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    ]
    assert {Path(source[1]).stem for source in membership} == {"same"}
    assert all(
        source.record.relative_path != "knowledge/notes/old.md"
        for source in published.snapshot.sources
    )


def test_registration_activation_and_the_manifest_agree(published):
    assert published.registered == published.active == published.manifest
    assert set(published.manifest) == {
        "generation_id",
        "schema_version",
        "collector_version",
        "extractor_version",
        "tokenizer_version",
        "tokenizer_config_sha256",
        "embedding_model_id",
        "embedding_model_revision",
        "vector_dimensions",
        "graph_schema_version",
        "graph_extractor_version",
        "source_manifest_sha256",
        "artifacts",
        "vector_state",
    }
    assert published.manifest["source_manifest_sha256"] == published.snapshot.corpus_sha256


def test_the_manifest_names_every_file_the_generation_wrote(published):
    on_disk = {
        path.relative_to(published.generation).as_posix()
        for path in published.generation.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }

    assert {item["path"] for item in published.manifest["artifacts"]} == on_disk


def test_the_search_index_carries_the_snapshot_chunks(published):
    with closing(sqlite3.connect(published.generation / "search.sqlite3")) as database:
        fts_manifest = database.execute(
            "SELECT value FROM generation_metadata WHERE key='source_manifest_sha256'"
        ).fetchone()[0]
        rows = database.execute(
            "SELECT chunk_id, source_id, source_path, source_sha256 "
            "FROM chunks ORDER BY chunk_order"
        ).fetchall()

    assert fts_manifest == published.snapshot.corpus_sha256
    assert [row[0] for row in rows] == [chunk.id for chunk in published.snapshot.chunks]
    assert [tuple(row[1:]) for row in rows] == _chunk_sources(published.snapshot)


def test_the_vectors_carry_the_snapshot_chunks(published, numpy_module):
    metadata = json.loads((published.generation / "vectors.json").read_bytes())
    vectors = numpy_module.load(published.generation / "vectors.npy", allow_pickle=False)

    assert metadata["corpus_sha256"] == published.snapshot.corpus_sha256
    assert metadata["chunk_ids"] == [chunk.id for chunk in published.snapshot.chunks]
    assert list(
        zip(
            metadata["source_ids"],
            metadata["source_paths"],
            metadata["source_sha256"],
            strict=True,
        )
    ) == _chunk_sources(published.snapshot)
    assert vectors.shape == (len(published.snapshot.chunks), VECTOR_DIMENSIONS)


def test_the_lance_rows_carry_the_snapshot_chunks(published):
    rows = json.loads((published.generation / "lance/rows.json").read_bytes())

    assert [row["chunk_id"] for row in rows] == [
        chunk.id for chunk in published.snapshot.chunks
    ]
    assert [
        (row["source_id"], row["source_path"], row["source_sha256"]) for row in rows
    ] == _chunk_sources(published.snapshot)


def test_the_contextual_and_tier_artifacts_carry_the_snapshot_sources(published):
    membership = _source_membership(published.snapshot)
    contextual = [
        (
            json.loads(path.read_bytes())["source"]["logical_id"],
            json.loads(path.read_bytes())["source"]["relative_path"],
            json.loads(path.read_bytes())["source"]["sha256"],
        )
        for path in sorted((published.generation / "contextual").glob("*.json"))
    ]
    entries = json.loads((published.generation / "tiers/tiers.json").read_bytes())["entries"]
    tiers = [
        (
            entry["source"]["logical_id"],
            entry["source"]["relative_path"],
            entry["source"]["sha256"],
        )
        for entry in entries
    ]

    assert sorted(contextual) == sorted(membership)
    assert tiers == membership


@pytest.mark.parametrize(
    "legacy",
    ("cache/index.sqlite", "cache/vectors.npy", "cache/vectors_meta.json", "cache/lancedb"),
)
def test_no_legacy_cache_is_left_beside_the_generation(published, tmp_path, legacy):
    assert not (published.vault / "cache").exists()
    assert {path.name for path in (tmp_path / "state/cache").iterdir()} == {
        "evidence-graph"
    }
    assert not (tmp_path / "state" / legacy).exists()


def test_publication_fence_preserves_prior_active_generation_on_hash_drift(
    tmp_path: Path,
    numpy_module,
):
    vault, source = _write_snapshot_vault(tmp_path)
    catalog = GenerationCatalog(tmp_path / "state")
    prior_snapshot = collect_corpus(vault)
    _prior_generation, prior_manifest = _build_generation(
        prior_snapshot, catalog, "generation-1"
    )
    catalog.register("generation-1")
    assert catalog.activate("generation-1", expected_active=None) is True

    source.write_text(
        _page(
            "First",
            "Candidate one.\n## Detail\nFirst detail.",
            type="concept",
            project="demo",
            source_authority="user",
            confidence="high",
        ),
        encoding="utf-8",
        newline="",
    )
    candidate_snapshot = collect_corpus(vault)
    _build_generation(candidate_snapshot, catalog, "generation-2")
    before = source.stat()
    source.write_text(
        _page(
            "First",
            "Candidate two.\n## Detail\nFirst detail.",
            type="concept",
            project="demo",
            source_authority="user",
            confidence="high",
        ),
        encoding="utf-8",
        newline="",
    )
    assert source.stat().st_size == before.st_size
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

    # Until 2026-09-05 a source edited while the build ran refused the whole
    # publication and left the prior generation active. On the live vault that
    # meant nothing was activated from 2026-08-30 onward. A snapshot describes a
    # moment the vault passed through, not the newest one, and a source that has
    # since moved on is dropped at query time before it can be quoted.
    published = search_memory.publish_generation(
        candidate_snapshot,
        vault,
        catalog,
        "generation-2",
        expected_active="generation-1",
    )

    assert published is True
    assert catalog.get_active() != prior_manifest
