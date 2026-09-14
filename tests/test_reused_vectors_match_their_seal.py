"""A parent's vectors are reused only when they hash to the digest its manifest seals.

A same-shape float32 matrix put in place of the parent's was copied row by row and sealed
again. Research: `docs/research/2026-09-14-reused-vectors-match-their-seal.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_generation_rebuild_reuse import (  # noqa: E402
    _PAGE,
    _CountingEmbedder,
    _seal_like_a_published_generation,
    _snapshot_from,
)


def _build(snapshot, directory: Path, embedder, reuse_from: Path | None = None) -> None:
    import search_memory

    directory.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot, directory, embedder=embedder, model_id="fixture/model", model_revision="rev-1", dimensions=4, reuse_from=reuse_from
    )


def test_a_same_shape_matrix_that_breaks_the_seal_is_not_reused(tmp_path):
    import numpy as np

    snapshot = _snapshot_from(tmp_path / "vault", {"a.md": _PAGE.format(title="Alpha", body="alpha body")})
    parent = tmp_path / "gen-1"
    _build(snapshot, parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    np.save(parent / "vectors.npy", np.ones((len(snapshot.chunks), 4), dtype=np.float32), allow_pickle=False)
    embedder = _CountingEmbedder()

    _build(snapshot, tmp_path / "gen-2", embedder, reuse_from=parent)

    assert len(embedder.encoded) == len(snapshot.chunks)


def test_sealed_vectors_are_still_reused(tmp_path):
    snapshot = _snapshot_from(tmp_path / "vault", {"a.md": _PAGE.format(title="Alpha", body="alpha body")})
    parent = tmp_path / "gen-1"
    _build(snapshot, parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    embedder = _CountingEmbedder()

    _build(snapshot, tmp_path / "gen-2", embedder, reuse_from=parent)

    assert embedder.encoded == []
