"""Tests for lance_store.py — LanceDB embedded vector backend."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class _FakeEmbedder:
    def __init__(self, dimensions: int = 3):
        self.dimensions = dimensions
        self.texts: list[str] = []

    def encode(self, texts, **_kwargs):
        self.texts = list(texts)
        return [
            [float(index + offset) for offset in range(self.dimensions)]
            for index, _text in enumerate(self.texts)
        ]


class _RecordingLanceAdapter:
    def __init__(self):
        self.output_dir: Path | None = None
        self.resolved_output_parent: Path | None = None
        self.rows: list[dict] = []
        self.dimensions: int | None = None

    def write(self, output_dir: Path, rows: list[dict], *, vector_dimension: int):
        self.output_dir = output_dir
        self.resolved_output_parent = output_dir.parent.resolve(strict=True)
        self.rows = rows
        self.dimensions = vector_dimension
        output_dir.mkdir(parents=True)
        payload = json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        (output_dir / "rows.json").write_bytes(payload)


def _page(title: str, body: str, **metadata: str) -> str:
    fields = "\n".join(f"{key}: {value}" for key, value in metadata.items())
    return f"---\n{fields}\n---\n# {title}\n{body}\n"


def _snapshot_vault(tmp_path: Path):
    from corpus_snapshot import collect_corpus

    vault = tmp_path / "vault"
    first = vault / "knowledge/notes/concept/same.md"
    second = vault / "knowledge/notes/pattern/same.md"
    superseded = vault / "knowledge/notes/old.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(
        _page(
            "First",
            "First evidence.",
            type="concept",
            project="demo",
            source_authority="user",
            confidence="high",
            language="en",
            valid_from="2026-01-01",
        ),
        encoding="utf-8",
    )
    second.write_text(
        _page("Second", "Second evidence.", type="pattern"), encoding="utf-8"
    )
    superseded.write_text(
        _page("Old", "Old evidence.", type="concept", status="superseded"),
        encoding="utf-8",
    )
    return vault, first, collect_corpus(vault)


def _generation_paths(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "generations"
    generation = root / "generation-1"
    generation.mkdir(parents=True)
    return root, generation


class TestGracefulDegradation:
    """LanceDB functions must degrade gracefully when not installed."""

    def test_have_lancedb_returns_bool(self):
        from lance_store import have_lancedb
        assert isinstance(have_lancedb(), bool)

    def test_vector_search_returns_empty_without_lancedb(self, monkeypatch):
        import lance_store

        monkeypatch.setattr(lance_store, "_get_db", lambda: None)
        results = lance_store.vector_search([0.1] * 384)
        assert isinstance(results, list)
        assert len(results) == 0

    def test_vector_count_returns_zero_without_lancedb(self, monkeypatch):
        import lance_store

        monkeypatch.setattr(lance_store, "_get_db", lambda: None)
        assert isinstance(lance_store.vector_count(), int)
        assert lance_store.vector_count() == 0

    def test_upsert_refuses_destructive_live_path_without_lancedb(self, monkeypatch):
        import lance_store

        monkeypatch.setattr(lance_store, "_get_db", lambda: None)
        with pytest.raises(RuntimeError, match="immutable generation"):
            lance_store.upsert_vectors(
                paths=["test"], titles=["T"], summaries=["S"],
                projects=["p"], timestamps=["2026-01-01"],
                vectors=[[0.1] * 384],
            )


class TestModuleStructure:
    """Verify module exports are correct."""

    def test_lancedb_dir_path(self):
        from lance_store import LANCEDB_DIR
        assert "lancedb" in str(LANCEDB_DIR).lower()
        assert "cache" in str(LANCEDB_DIR).lower()

    def test_table_name(self):
        from lance_store import TABLE_NAME
        assert TABLE_NAME == "pages_vec"

    def test_embedding_dim(self):
        from lance_store import EMBEDDING_DIM
        assert EMBEDDING_DIM == 384

    def test_all_functions_exist(self):
        from lance_store import (
            have_lancedb,
            upsert_vectors,
            vector_count,
            vector_search,
        )
        assert callable(have_lancedb)
        assert callable(upsert_vectors)
        assert callable(vector_search)
        assert callable(vector_count)


class TestSnapshotLanceGeneration:
    def test_consumes_snapshot_chunks_in_order_with_full_identity_and_metadata(
        self, tmp_path
    ):
        from rebuild_lance_index import build_lance_generation

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        embedder = _FakeEmbedder()
        adapter = _RecordingLanceAdapter()

        build_lance_generation(
            snapshot,
            generation,
            generation_root=generation_root,
            embedder=embedder,
            embedding_model_id="fake/model",
            embedding_model_revision="commit-123",
            embedding_dimensions=3,
            lance_adapter=adapter,
        )

        assert embedder.texts == [chunk.text for chunk in snapshot.chunks]
        assert [row["chunk_id"] for row in adapter.rows] == [
            chunk.id for chunk in snapshot.chunks
        ]
        assert [row["source_id"] for row in adapter.rows] == [
            chunk.source_id for chunk in snapshot.chunks
        ]
        assert [row["source_path"] for row in adapter.rows] == [
            chunk.source_path for chunk in snapshot.chunks
        ]
        assert {row["source_path"] for row in adapter.rows} == {
            "knowledge/notes/concept/same.md",
            "knowledge/notes/pattern/same.md",
        }
        assert adapter.rows[0] | {"vector": None} == {
            "chunk_id": snapshot.chunks[0].id,
            "source_id": snapshot.chunks[0].source_id,
            "source_path": "knowledge/notes/concept/same.md",
            "parent_page": snapshot.chunks[0].parent_page,
            "heading_ancestry": ["First"],
            "byte_start": snapshot.chunks[0].byte_start,
            "byte_end": snapshot.chunks[0].byte_end,
            "line_start": snapshot.chunks[0].line_start,
            "line_end": snapshot.chunks[0].line_end,
            "text": snapshot.chunks[0].text,
            "source_sha256": snapshot.chunks[0].source_sha256,
            "span_sha256": snapshot.chunks[0].span_sha256,
            "type": "concept",
            "project": "demo",
            "authority": "user",
            "confidence": "high",
            "status": "active",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "language": "en",
            "embedding_model_id": "fake/model",
            "embedding_model_revision": "commit-123",
            "embedding_dimensions": 3,
            "vector": None,
        }

    def test_does_not_reread_live_markdown_or_call_legacy_mutable_store(
        self, tmp_path, monkeypatch
    ):
        import lance_store
        from rebuild_lance_index import build_lance_generation

        _vault, source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        source.unlink()
        monkeypatch.setattr(
            lance_store,
            "upsert_vectors",
            lambda *_args, **_kwargs: pytest.fail("legacy mutable store was called"),
        )
        adapter = _RecordingLanceAdapter()

        build_lance_generation(
            snapshot,
            generation,
            generation_root=generation_root,
            embedder=_FakeEmbedder(),
            embedding_model_id="fake/model",
            embedding_model_revision="commit-123",
            embedding_dimensions=3,
            lance_adapter=adapter,
        )

        assert [row["text"] for row in adapter.rows] == [
            chunk.text for chunk in snapshot.chunks
        ]

    def test_returns_deterministic_bounded_generation_relative_artifacts(
        self, tmp_path
    ):
        from rebuild_lance_index import build_lance_generation

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        adapter = _RecordingLanceAdapter()

        artifacts = build_lance_generation(
            snapshot,
            generation,
            generation_root=generation_root,
            embedder=_FakeEmbedder(),
            embedding_model_id="fake/model",
            embedding_model_revision="commit-123",
            embedding_dimensions=3,
            lance_adapter=adapter,
        )

        artifact = generation / "lance/rows.json"
        assert adapter.output_dir is not None
        assert adapter.output_dir.name == "lance"
        if sys.platform.startswith("linux"):
            assert adapter.output_dir.parts[:4] == ("/", "proc", "self", "fd")
            assert adapter.output_dir.parts[4].isdigit()
            assert adapter.resolved_output_parent is not None
            assert adapter.resolved_output_parent.parent == generation_root
            assert adapter.resolved_output_parent.name.startswith(".lance-build-")
        else:
            assert adapter.output_dir.is_relative_to(generation_root)
        assert artifacts == [
            {
                "path": "lance/rows.json",
                "size": artifact.stat().st_size,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ]
        assert not (tmp_path / "cache/lancedb").exists()
        assert all("fake/model" == row["embedding_model_id"] for row in adapter.rows)
        assert all(
            "commit-123" == row["embedding_model_revision"] for row in adapter.rows
        )

    def test_unavailable_dependency_leaves_no_partial_generation_artifact(
        self, tmp_path, monkeypatch
    ):
        import rebuild_lance_index

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        monkeypatch.setattr(
            rebuild_lance_index,
            "_default_lance_adapter",
            lambda: (_ for _ in ()).throw(RuntimeError("LanceDB is not installed")),
        )

        with pytest.raises(RuntimeError, match="LanceDB is not installed"):
            rebuild_lance_index.build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
            )

        assert list(generation.iterdir()) == []

    def test_accepts_numpy_embedding_matrix_from_sentence_transformers(self, tmp_path):
        numpy = pytest.importorskip("numpy")
        from rebuild_lance_index import build_lance_generation

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        embedder = _FakeEmbedder()
        embedder.encode = lambda texts: numpy.asarray(
            [[1.0, 2.0, 3.0] for _text in texts], dtype=numpy.float32
        )
        adapter = _RecordingLanceAdapter()

        build_lance_generation(
            snapshot,
            generation,
            generation_root=generation_root,
            embedder=embedder,
            embedding_model_id="fake/model",
            embedding_model_revision="commit-123",
            embedding_dimensions=3,
            lance_adapter=adapter,
        )

        assert adapter.rows[0]["vector"] == [1.0, 2.0, 3.0]

    def test_rejects_symlink_generation_before_adapter_and_never_touches_target(
        self, tmp_path
    ):
        from rebuild_lance_index import build_lance_generation

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root = tmp_path / "generations"
        redirected = tmp_path / "redirected"
        generation_root.mkdir()
        redirected.mkdir()
        marker = redirected / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        generation = generation_root / "generation-1"
        try:
            generation.symlink_to(redirected, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                pytest.skip(f"directory symlinks unavailable: {exc}")
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(generation), str(redirected)],
                check=True,
                capture_output=True,
            )
        adapter = _RecordingLanceAdapter()

        with pytest.raises(PermissionError, match="real directory"):
            build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=adapter,
            )

        assert adapter.output_dir is None
        assert marker.read_text(encoding="utf-8") == "keep"
        assert not (redirected / "lance").exists()

    def test_artifact_scan_stops_at_entry_bound_without_rglob(
        self, tmp_path, monkeypatch
    ):
        import rebuild_lance_index

        class ManyEntriesAdapter:
            def write(self, output_dir, rows, *, vector_dimension):
                del rows, vector_dimension
                output_dir.mkdir()
                for index in range(4):
                    (output_dir / f"{index}.bin").write_bytes(b"x")

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        real_scandir = os.scandir
        scanned_entries = 0
        first_artifact_scan = True

        class CountingScandir:
            def __init__(self, iterator):
                self.iterator = iterator

            def __enter__(self):
                self.iterator.__enter__()
                return self

            def __exit__(self, *args):
                return self.iterator.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal scanned_entries
                entry = next(self.iterator)
                scanned_entries += 1
                return entry

        def counting_scandir(path):
            nonlocal first_artifact_scan
            descriptor_scan = os.name == "posix" and isinstance(path, int)
            pathname_scan = os.name != "posix" and Path(path) == generation / "lance"
            if (descriptor_scan or pathname_scan) and first_artifact_scan:
                first_artifact_scan = False
                return CountingScandir(real_scandir(path))
            return real_scandir(path)

        monkeypatch.setattr(rebuild_lance_index, "MAX_LANCE_ENTRIES", 2)
        monkeypatch.setattr(rebuild_lance_index.os, "scandir", counting_scandir)
        monkeypatch.setattr(
            Path,
            "rglob",
            lambda *_args, **_kwargs: pytest.fail("unbounded rglob was called"),
        )

        with pytest.raises(ValueError, match="entry count"):
            rebuild_lance_index.build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=ManyEntriesAdapter(),
            )

        assert list(generation.iterdir()) == []
        assert scanned_entries == 3

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("embedding_model_id", "x" * 129, "bounded non-empty string"),
            ("embedding_model_revision", "bad\nrevision", "bounded non-empty string"),
            ("embedding_dimensions", 65537, "positive bounded integer"),
        ],
    )
    def test_model_metadata_matches_generation_catalog_bounds(
        self, tmp_path, field, value, message
    ):
        from rebuild_lance_index import build_lance_generation

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        arguments = {
            "embedding_model_id": "fake/model",
            "embedding_model_revision": "commit-123",
            "embedding_dimensions": 3,
        }
        arguments[field] = value

        with pytest.raises(ValueError, match=message):
            build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                lance_adapter=_RecordingLanceAdapter(),
                **arguments,
            )

        assert list(generation.iterdir()) == []

    def test_partial_adapter_failure_preserves_exception_and_cleans_artifacts(
        self, tmp_path, monkeypatch
    ):
        import rebuild_lance_index

        failure = RuntimeError("adapter failed after write")

        class PartialAdapter:
            def write(self, output_dir, rows, *, vector_dimension):
                del rows, vector_dimension
                output_dir.mkdir()
                (output_dir / "partial.bin").write_bytes(b"partial")
                raise failure

        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        real_cleanup = rebuild_lance_index._cleanup_output

        def cleanup_then_fail(*args):
            real_cleanup(*args)
            raise OSError("cleanup failed")

        monkeypatch.setattr(rebuild_lance_index, "_cleanup_output", cleanup_then_fail)

        with pytest.raises(RuntimeError) as raised:
            rebuild_lance_index.build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=PartialAdapter(),
            )

        assert raised.value is failure
        assert list(generation.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
    def test_posix_child_replacement_never_traverses_or_deletes_redirect(
        self, tmp_path, monkeypatch
    ):
        import rebuild_lance_index

        external = tmp_path / "external"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        output_seen = None

        class NestedAdapter:
            def write(self, output_dir, rows, *, vector_dimension):
                nonlocal output_seen
                del rows, vector_dimension
                output_seen = output_dir
                child = output_dir / "child"
                child.mkdir(parents=True)
                (child / "artifact.bin").write_bytes(b"artifact")

        real_open = rebuild_lance_index._open_posix_directory_at
        replaced = False

        def replace_before_open(parent_fd, name, expected):
            nonlocal replaced
            if name == "child" and not replaced:
                replaced = True
                assert output_seen is not None
                child = output_seen / name
                child.rename(output_seen / "original-child")
                child.symlink_to(external, target_is_directory=True)
            return real_open(parent_fd, name, expected)

        monkeypatch.setattr(
            rebuild_lance_index, "_open_posix_directory_at", replace_before_open
        )
        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)

        with pytest.raises(PermissionError, match="changed|linked"):
            rebuild_lance_index.build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=NestedAdapter(),
            )

        assert marker.read_text(encoding="utf-8") == "keep"
        assert not (external / "artifact.bin").exists()
        assert not (generation / "lance").exists()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
    def test_posix_generation_replacement_cannot_publish_into_redirect(
        self, tmp_path
    ):
        from rebuild_lance_index import build_lance_generation

        external = tmp_path / "external"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)

        class ReplacingAdapter:
            def write(self, output_dir, rows, *, vector_dimension):
                del rows, vector_dimension
                output_dir.mkdir()
                (output_dir / "artifact.bin").write_bytes(b"artifact")
                moved = generation.with_name("generation-moved")
                generation.rename(moved)
                generation.symlink_to(external, target_is_directory=True)

        with pytest.raises(PermissionError, match="generation.*changed"):
            build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=ReplacingAdapter(),
            )

        assert marker.read_text(encoding="utf-8") == "keep"
        assert not (external / "lance").exists()
        assert not (generation_root / "generation-moved/lance").exists()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor semantics")
    def test_posix_generation_replaced_before_adapter_open_cannot_redirect_writes(
        self, tmp_path
    ):
        from rebuild_lance_index import build_lance_generation

        external = tmp_path / "external"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        _vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)

        class ReplaceBeforeOpenAdapter:
            def write(self, output_dir, rows, *, vector_dimension):
                del rows, vector_dimension
                moved = generation.with_name("generation-moved")
                generation.rename(moved)
                generation.symlink_to(external, target_is_directory=True)
                output_dir.mkdir(parents=True)
                (output_dir / "artifact.bin").write_bytes(b"artifact")

        with pytest.raises(PermissionError, match="generation.*changed"):
            build_lance_generation(
                snapshot,
                generation,
                generation_root=generation_root,
                embedder=_FakeEmbedder(),
                embedding_model_id="fake/model",
                embedding_model_revision="commit-123",
                embedding_dimensions=3,
                lance_adapter=ReplaceBeforeOpenAdapter(),
            )

        assert list(external.iterdir()) == [marker]
        assert marker.read_text(encoding="utf-8") == "keep"
        assert not (generation_root / "generation-moved/lance").exists()
        assert not any(
            path.name.startswith(".lance-build-")
            for path in generation_root.iterdir()
            if not path.is_symlink()
        )


class TestGenerationCli:
    def test_generation_mode_collects_one_snapshot_and_builds_unpublished(
        self, tmp_path, monkeypatch, capsys
    ):
        import rebuild_lance_index

        vault, _source, snapshot = _snapshot_vault(tmp_path)
        generation_root, generation = _generation_paths(tmp_path)
        collected = []
        built = []
        embedder = object()

        def collect(root):
            collected.append(root)
            return snapshot

        monkeypatch.setattr(rebuild_lance_index, "ROOT", vault)
        monkeypatch.setattr(rebuild_lance_index, "collect_corpus", collect)
        monkeypatch.setattr(
            rebuild_lance_index,
            "_load_generation_embedder",
            lambda model_id, revision: (
                embedder
                if (model_id, revision) == ("fake/model", "commit-123")
                else pytest.fail("wrong model metadata")
            ),
        )
        monkeypatch.setattr(
            rebuild_lance_index,
            "build_lance_generation",
            lambda *args, **kwargs: built.append((args, kwargs))
            or [{"path": "lance/data", "size": 1, "sha256": "0" * 64}],
        )

        result = rebuild_lance_index.main(
            [
                "--generation-root",
                str(generation_root),
                "--generation-dir",
                str(generation),
                "--embedding-model-id",
                "fake/model",
                "--embedding-model-revision",
                "commit-123",
                "--embedding-dimensions",
                "3",
            ]
        )

        assert result == 0
        assert collected == [vault]
        assert len(built) == 1
        assert built[0][0] == (snapshot, generation)
        assert built[0][1] == {
            "generation_root": generation_root,
            "embedder": embedder,
            "embedding_model_id": "fake/model",
            "embedding_model_revision": "commit-123",
            "embedding_dimensions": 3,
        }
        output = capsys.readouterr().out
        assert "unpublished" in output.lower()
        assert "activated" not in output.lower()

    def test_default_cli_is_clearly_labeled_legacy_compatibility(
        self, monkeypatch, capsys
    ):
        import rebuild_lance_index

        monkeypatch.setattr(rebuild_lance_index, "rebuild_lance", lambda: {"pages": 1})

        assert rebuild_lance_index.main([]) == 0
        assert "legacy compatibility" in capsys.readouterr().out.lower()


class TestSearchIntegration:
    """Test search_memory.py falls back correctly when LanceDB unavailable."""

    def test_search_works_without_lancedb(self):
        """Search must work with numpy fallback when LanceDB not installed."""
        from search_memory import search
        results = search("test", limit=5)
        assert isinstance(results, list)

    def test_search_semantic_works_without_lancedb(self):
        from search_memory import search
        results = search("test", limit=5, semantic=True)
        assert isinstance(results, list)
