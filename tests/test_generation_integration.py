"""Cross-consumer integration tests for one immutable corpus generation."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_tiers  # noqa: E402
import contextual_retrieval  # noqa: E402
import rebuild_lance_index  # noqa: E402
import search_memory  # noqa: E402
from corpus_snapshot import CorpusChanged, collect_corpus  # noqa: E402
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


def test_all_generation_consumers_publish_one_closed_snapshot(tmp_path: Path):
    vault, _source = _write_snapshot_vault(tmp_path)
    snapshot = collect_corpus(vault)
    catalog = GenerationCatalog(tmp_path / "state")

    generation, manifest = _build_generation(snapshot, catalog, "generation-1")
    registered = catalog.register("generation-1")
    assert catalog.activate("generation-1", expected_active=None) is True
    active = catalog.get_active()

    expected_sources = _source_membership(snapshot)
    expected_chunk_ids = [chunk.id for chunk in snapshot.chunks]
    expected_chunk_sources = [
        (chunk.source_id, chunk.source_path, chunk.source_sha256)
        for chunk in snapshot.chunks
    ]
    assert [source[1] for source in expected_sources] == [
        "knowledge/notes/concept/same.md",
        "knowledge/notes/pattern/same.md",
    ]
    assert {Path(source[1]).stem for source in expected_sources} == {"same"}
    assert all(source.record.relative_path != "knowledge/notes/old.md" for source in snapshot.sources)
    assert registered == active == manifest
    assert set(manifest) == {
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
    assert manifest["source_manifest_sha256"] == snapshot.corpus_sha256
    assert {item["path"] for item in manifest["artifacts"]} == {
        path.relative_to(generation).as_posix()
        for path in generation.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }

    with closing(sqlite3.connect(generation / "search.sqlite3")) as database:
        fts_manifest = database.execute(
            "SELECT value FROM generation_metadata WHERE key='source_manifest_sha256'"
        ).fetchone()[0]
        fts_chunks = database.execute(
            "SELECT chunk_id, source_id, source_path, source_sha256 "
            "FROM chunks ORDER BY chunk_order"
        ).fetchall()
    assert fts_manifest == snapshot.corpus_sha256
    assert [row[0] for row in fts_chunks] == expected_chunk_ids
    assert [tuple(row[1:]) for row in fts_chunks] == expected_chunk_sources

    vector_metadata = json.loads((generation / "vectors.json").read_bytes())
    vectors = np.load(generation / "vectors.npy", allow_pickle=False)
    assert vector_metadata["corpus_sha256"] == snapshot.corpus_sha256
    assert vector_metadata["chunk_ids"] == expected_chunk_ids
    assert list(
        zip(
            vector_metadata["source_ids"],
            vector_metadata["source_paths"],
            vector_metadata["source_sha256"],
            strict=True,
        )
    ) == expected_chunk_sources
    assert vectors.shape == (len(snapshot.chunks), VECTOR_DIMENSIONS)

    lance_rows = json.loads((generation / "lance/rows.json").read_bytes())
    assert [row["chunk_id"] for row in lance_rows] == expected_chunk_ids
    assert [
        (row["source_id"], row["source_path"], row["source_sha256"])
        for row in lance_rows
    ] == expected_chunk_sources

    context_sources = []
    for path in sorted((generation / "contextual").glob("*.json")):
        source = json.loads(path.read_bytes())["source"]
        context_sources.append(
            (source["logical_id"], source["relative_path"], source["sha256"])
        )
    assert sorted(context_sources) == sorted(expected_sources)

    tier_entries = json.loads((generation / "tiers/tiers.json").read_bytes())["entries"]
    tier_sources = [
        (
            entry["source"]["logical_id"],
            entry["source"]["relative_path"],
            entry["source"]["sha256"],
        )
        for entry in tier_entries
    ]
    assert tier_sources == expected_sources

    assert not (vault / "cache").exists()
    assert {path.name for path in (tmp_path / "state/cache").iterdir()} == {
        "evidence-graph"
    }
    for legacy in (
        "cache/index.sqlite",
        "cache/vectors.npy",
        "cache/vectors_meta.json",
        "cache/lancedb",
    ):
        assert not (tmp_path / "state" / legacy).exists()


def test_publication_fence_preserves_prior_active_generation_on_hash_drift(
    tmp_path: Path,
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

    with pytest.raises(CorpusChanged, match="membership or source hashes changed"):
        search_memory.publish_generation(
            candidate_snapshot,
            vault,
            catalog,
            "generation-2",
            expected_active="generation-1",
        )

    assert catalog.get_active() == prior_manifest
    with pytest.raises(ValueError, match="generation is not registered"):
        catalog.activate("generation-2", expected_active="generation-1")
