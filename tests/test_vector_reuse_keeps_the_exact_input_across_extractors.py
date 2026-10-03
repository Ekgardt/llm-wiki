"""An extraction namespace change must not re-encode identical proven model input."""
from dataclasses import replace

import corpus_snapshot as corpus
import numpy as np
import search_memory as search

from tests.test_generation_rebuild_reuse import (
    _PAGE,
    _CountingEmbedder,
    _seal_like_a_published_generation,
    _snapshot_from,
)


def _at_version(snapshot, version):
    chunks = tuple(chunk for source in snapshot.sources for chunk in corpus.canonical_retrieval_chunks(
        source_id=source.record.logical_id, source_path=source.record.relative_path,
        source_sha256=source.record.sha256, content=source.content, extractor_version=version,
    ))
    digest = corpus.canonical_source_manifest_sha256(
        (source.record for source in snapshot.sources), snapshot.policy,
        collector_version=snapshot.collector_version, extractor_version=version,
    )
    return replace(snapshot, chunks=chunks, corpus_sha256=digest, extractor_version=version)


def _build(snapshot, directory, embedder, parent=None, revision='rev-1'):
    directory.mkdir()
    search.build_generation_numpy_vectors(
        snapshot, directory, embedder=embedder, model_id='fixture/model',
        model_revision=revision, dimensions=4, reuse_from=parent,
    )


def test_only_the_extractor_namespace_changes_no_model_input(tmp_path):
    snapshot = _snapshot_from(tmp_path / 'vault', {'a.md': _PAGE.format(title='Alpha', body='same 字 input')})
    old = _at_version(snapshot, 'test-extractor/old')
    new = _at_version(snapshot, 'test-extractor/new')
    assert [chunk.id for chunk in old.chunks] != [chunk.id for chunk in new.chunks]
    parent = tmp_path / 'parent'
    _build(old, parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    embedder = _CountingEmbedder()
    child = tmp_path / 'child'
    _build(new, child, embedder, parent)
    assert embedder.encoded == []
    assert np.array_equal(np.load(parent / 'vectors.npy', allow_pickle=False), np.load(child / 'vectors.npy', allow_pickle=False))


def test_a_model_revision_change_is_still_fresh(tmp_path):
    snapshot = _snapshot_from(tmp_path / 'vault', {'a.md': _PAGE.format(title='Alpha', body='same input')})
    parent = tmp_path / 'parent'
    _build(_at_version(snapshot, 'test/old'), parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    embedder = _CountingEmbedder()
    _build(_at_version(snapshot, 'test/new'), tmp_path / 'child', embedder, parent, revision='rev-2')
    assert embedder.encoded == [chunk.text for chunk in snapshot.chunks]


def test_changed_source_is_encoded_while_unchanged_source_is_reused(tmp_path):
    pages = {'a.md': _PAGE.format(title='Alpha', body='same'), 'b.md': _PAGE.format(title='Beta', body='before')}
    first = _snapshot_from(tmp_path / 'vault', pages)
    parent = tmp_path / 'parent'
    _build(_at_version(first, 'test/old'), parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    pages['b.md'] = _PAGE.format(title='Beta', body='after')
    second = _at_version(_snapshot_from(tmp_path / 'vault', pages), 'test/new')
    embedder = _CountingEmbedder()
    _build(second, tmp_path / 'child', embedder, parent)
    assert embedder.encoded == [chunk.text for chunk in second.chunks if chunk.source_path.endswith('b.md')]


def test_a_broken_parent_seal_cannot_be_reused_across_extractors(tmp_path):
    snapshot = _snapshot_from(tmp_path / 'vault', {'a.md': _PAGE.format(title='Alpha', body='same')})
    parent = tmp_path / 'parent'
    _build(_at_version(snapshot, 'test/old'), parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    np.save(parent / 'vectors.npy', np.ones((len(snapshot.chunks), 4), dtype=np.float32), allow_pickle=False)
    embedder = _CountingEmbedder()
    _build(_at_version(snapshot, 'test/new'), tmp_path / 'child', embedder, parent)
    assert embedder.encoded == [chunk.text for chunk in snapshot.chunks]


def test_changed_text_with_an_unchanged_declared_identity_is_not_reused(tmp_path):
    snapshot = _snapshot_from(tmp_path / 'vault', {'a.md': _PAGE.format(title='Alpha', body='same')})
    parent = tmp_path / 'parent'
    _build(_at_version(snapshot, 'test/old'), parent, _CountingEmbedder())
    _seal_like_a_published_generation(parent)
    child = _at_version(snapshot, 'test/new')
    changed = replace(child.chunks[0], text='changed bytes')
    child = replace(child, chunks=(changed,))
    embedder = _CountingEmbedder()
    _build(child, tmp_path / 'child', embedder, parent)
    assert embedder.encoded == ['changed bytes']
