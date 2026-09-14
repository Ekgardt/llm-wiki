"""A generation whose chunks are all reused does not load the embedding model.

The model was loaded (6.8 s, 1.2 GB) before the parent's rows were consulted, on nights
when no chunk needed a vector. Research:
`docs/research/2026-09-14-the-model-loads-only-for-new-chunks.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_generation_rebuild_reuse import _PAGE, _snapshot_from  # noqa: E402


def _wide_embedder(texts):
    """A deterministic stand-in as wide as the real model."""
    import search_memory

    return [[(index + len(text)) % 97 / 97 for index in range(search_memory.EMBEDDING_DIM)] for text in texts]


def _parent_generation(tmp_path: Path):
    import search_memory

    snapshot = _snapshot_from(tmp_path / "vault", {"a.md": _PAGE.format(title="Alpha", body="alpha body")})
    parent = tmp_path / "gen-1"
    parent.mkdir()
    search_memory.build_generation_numpy_vectors(
        snapshot,
        parent,
        embedder=_wide_embedder,
        model_id=search_memory.EMBEDDING_MODEL,
        model_revision=search_memory.EMBEDDING_MODEL_REVISION,
        dimensions=search_memory.EMBEDDING_DIM,
    )
    return snapshot, parent


def test_an_unchanged_corpus_builds_its_vectors_without_the_model(tmp_path, monkeypatch):
    import search_memory

    snapshot, parent = _parent_generation(tmp_path)
    monkeypatch.setattr(search_memory, "_have_sentence_transformers", lambda: True)
    monkeypatch.setattr(search_memory, "_get_embedder", lambda: pytest.fail("the model must not load"))
    child = tmp_path / "gen-2"
    child.mkdir()

    built = search_memory.build_generation_vectors_if_available(snapshot, child, reuse_from=parent)

    assert (built["reused_chunks"], built["embedded_chunks"]) == (len(snapshot.chunks), 0)


def test_a_model_that_cannot_load_when_needed_still_means_no_vectors(tmp_path, monkeypatch):
    import search_memory

    snapshot = _snapshot_from(tmp_path / "vault", {"a.md": _PAGE.format(title="Alpha", body="alpha body")})
    monkeypatch.setattr(search_memory, "_have_sentence_transformers", lambda: True)
    monkeypatch.setattr(search_memory, "_get_embedder", lambda: None)
    child = tmp_path / "gen-1"
    child.mkdir()

    assert search_memory.build_generation_vectors_if_available(snapshot, child) is None
