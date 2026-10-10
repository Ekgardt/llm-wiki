"""Persisted graph working rows must not overlap the vector encoder's lifetime."""
import weakref

import evidence_graph_builder as builder
import pytest
from generation_catalog import GenerationCatalog

from tests.test_evidence_graph_incremental import (
    FixtureExtractor,
    _build,
    _document,
    _snapshot,
    _tables,
)


class TrackedRow(dict):
    pass


def watched_row(row, watches):
    tracked = TrackedRow(row)
    watches.append(weakref.ref(tracked))
    return tracked


def watched_rows(rows, watches):
    for row in rows:
        yield watched_row(row, watches)


def test_full_build_releases_owned_graph_rows_before_vectors(tmp_path, monkeypatch):
    files = {'lib': ('lib.fixture', _document('lib'))}
    sources, contents = _snapshot(files)
    result = FixtureExtractor()(sources[0], contents['lib'], sources=sources,
                               source_bytes=contents, deadline=None, cancelled=None)
    watches = []
    original = builder._generation_vector_artifacts

    def observe(*args, **kwargs):
        assert watches and all(watch() is None for watch in watches)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder, '_generation_vector_artifacts', observe)
    built = builder.build_full_generation(
        GenerationCatalog(tmp_path), sources=sources, source_bytes=contents,
        nodes=watched_rows(result.nodes, watches),
        occurrences=watched_rows(result.occurrences, watches),
        assertions=(), evidence=(), observations=(), dependencies=(), generation_id='full',
    )
    assert built.activated
    assert len(_tables(built.generation_path / 'evidence.sqlite3')['node']) == 1


def watch_owned_merge(original, watches):
    def merge(*args, **kwargs):
        merged, ownership = original(*args, **kwargs)
        for collection, records in merged.items():
            merged[collection] = {key: watched_row(row, watches) for key, row in records.items()}
        return merged, ownership
    return merge


def watch_extractor_owner(original, owners):
    def create(*args, **kwargs):
        runner = original(*args, **kwargs)
        owners.append(weakref.ref(runner))
        return runner
    return create


@pytest.mark.parametrize('reuse_parent', [False, True])
def test_incremental_releases_owned_merge_and_extractor_before_vectors(tmp_path, monkeypatch, reuse_parent):
    catalog = GenerationCatalog(tmp_path)
    files = {'lib': ('lib.fixture', _document('lib')),
             'consumer': ('consumer.fixture', _document('consumer', imports=('lib',)))}
    parent = None
    if reuse_parent:
        _build(catalog, 'parent', files, FixtureExtractor())
        parent = 'parent'
    watches, owners = [], []
    original_vectors = builder._generation_vector_artifacts
    monkeypatch.setattr(builder, '_merged_records', watch_owned_merge(builder._merged_records, watches))
    monkeypatch.setattr(builder, '_IncrementalExtractor', watch_extractor_owner(builder._IncrementalExtractor, owners))

    def observe(*args, **kwargs):
        assert watches and all(watch() is None for watch in watches)
        assert owners and all(owner() is None for owner in owners)
        return original_vectors(*args, **kwargs)

    monkeypatch.setattr(builder, '_generation_vector_artifacts', observe)
    built = _build(catalog, 'candidate', files, FixtureExtractor(), parent=parent)
    assert built.activated
    assert len(_tables(built.generation_path / 'evidence.sqlite3')['node']) == 2
    assert built.reused_sources == (('consumer', 'lib') if reuse_parent else ())
