"""Query reuse needs current byte authority; a manifest digest alone is no proof."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing

import pytest
import retrieval
import search_memory

from tests.test_search_ranking import _unregistered_vector_generation


def prepared(tmp_path, monkeypatch):
    directory, manifest, catalog = _unregistered_vector_generation(tmp_path)
    catalog.state_root = tmp_path
    search_memory.validate_generation_fts_artifact(directory, manifest, state_root=tmp_path, deep=False)
    monkeypatch.setattr(search_memory, '_VALID_FTS_ARTIFACTS', OrderedDict())
    seal = search_memory._generation_consumption_seal(catalog, manifest, ('search.sqlite3',))
    return directory, manifest, catalog, seal


def observe_validation(monkeypatch):
    calls = []
    original = search_memory._valid_generation_fts

    def inspect(*args, **kwargs):
        calls.append(kwargs.get('check_rows', True))
        return original(*args, **kwargs)

    monkeypatch.setattr(search_memory, '_valid_generation_fts', inspect)
    return calls


def query(catalog, manifest, seal, **stop):
    return retrieval._generation_connection_for(search_memory, catalog, manifest, seal, stop)


def test_current_sealed_artifact_reuses_persisted_exact_row_proof(tmp_path, monkeypatch):
    _directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    calls = observe_validation(monkeypatch)
    with closing(query(catalog, manifest, seal)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM chunks').fetchone()[0] > 0
    assert calls == [False]
    assert not search_memory._VALID_FTS_ARTIFACTS


def test_raw_connection_without_source_seal_remains_strict(tmp_path, monkeypatch):
    _directory, manifest, catalog, _seal = prepared(tmp_path, monkeypatch)
    calls = observe_validation(monkeypatch)
    with closing(search_memory._generation_connection(catalog, manifest)) as connection:
        assert connection is not None
    assert calls == [True]


def test_absent_persistent_verdict_still_checks_rows(tmp_path, monkeypatch):
    _directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    from verified_artifacts import cache_path

    cache_path(tmp_path).unlink()
    calls = observe_validation(monkeypatch)
    with closing(query(catalog, manifest, seal)) as connection:
        assert connection is not None
    assert calls == [True]


@pytest.mark.parametrize('seal', [(), ('forged', None, ()), ('0' * 64, None, ())])
def test_unverified_seal_cannot_grant_persistent_row_reuse(tmp_path, monkeypatch, seal):
    _directory, manifest, catalog, _actual = prepared(tmp_path, monkeypatch)
    calls = observe_validation(monkeypatch)
    connection = query(catalog, manifest, seal)
    assert connection is None or calls == [True]
    if connection is not None:
        connection.close()


def test_cancelled_query_never_reuses_a_verdict(tmp_path, monkeypatch):
    _directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    with pytest.raises(TimeoutError, match='cancel'):
        query(catalog, manifest, seal, cancelled=lambda: True)


def test_changed_file_after_original_seal_is_refused(tmp_path, monkeypatch):
    directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    artifact = directory / 'search.sqlite3'
    artifact.write_bytes(artifact.read_bytes() + b'changed bytes')
    assert query(catalog, manifest, seal) is None


def test_unknown_schema_cannot_reuse_known_artifact_rows(tmp_path, monkeypatch):
    _directory, manifest, catalog, _seal = prepared(tmp_path, monkeypatch)
    manifest['extractor_version'] = 'unrecognized-extractor'
    seal = search_memory._generation_consumption_seal(catalog, manifest, ('search.sqlite3',))
    assert query(catalog, manifest, seal) is None


def test_expired_caller_is_not_a_successful_cached_read(tmp_path, monkeypatch):
    _directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    with pytest.raises(TimeoutError, match='deadline'):
        query(catalog, manifest, seal, deadline=0)


def replacing_validation(monkeypatch, artifact, *, replace):
    original = search_memory._valid_generation_fts
    closed = []
    close = search_memory._close_quietly

    def inspect(*args, **kwargs):
        answer = original(*args, **kwargs)
        replace(artifact)
        return answer

    def observe_close(connection):
        closed.append(connection)
        close(connection)

    monkeypatch.setattr(search_memory, '_valid_generation_fts', inspect)
    monkeypatch.setattr(search_memory, '_close_quietly', observe_close)
    return closed


def swap_bytes(artifact):
    replacement = artifact.with_name('replacement.sqlite3')
    replacement.write_bytes(artifact.read_bytes())
    replacement.replace(artifact)


def change_bytes_preserving_mtime(artifact):
    import os

    before = artifact.stat()
    content = artifact.read_bytes()
    artifact.write_bytes(content + b'changed')
    os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize('replace', [swap_bytes, change_bytes_preserving_mtime])
def test_mutation_during_open_validation_closes_and_refuses(tmp_path, monkeypatch, replace):
    import sqlite3

    directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    closed = replacing_validation(monkeypatch, directory / 'search.sqlite3', replace=replace)
    assert query(catalog, manifest, seal) is None
    assert len(closed) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        closed[0].execute('SELECT 1')


def test_corrupt_new_digest_is_walked_and_refused(tmp_path, monkeypatch):
    import sqlite3

    from tests.test_search_ranking import _refresh_artifact_descriptor

    directory, manifest, catalog, _seal = prepared(tmp_path, monkeypatch)
    artifact = directory / 'search.sqlite3'
    with sqlite3.connect(artifact) as connection:
        connection.execute('UPDATE chunks SET chunk_order = 999')
    _refresh_artifact_descriptor(manifest, artifact)
    seal = search_memory._generation_consumption_seal(catalog, manifest, ('search.sqlite3',))
    calls = observe_validation(monkeypatch)
    assert query(catalog, manifest, seal) is None
    assert calls == [True]


def test_manifest_change_during_reuse_closes_and_refuses(tmp_path, monkeypatch):
    directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)

    def change_manifest(_artifact):
        manifest['generation_id'] = 'different-generation'

    closed = replacing_validation(monkeypatch, directory / 'search.sqlite3', replace=change_manifest)
    assert query(catalog, manifest, seal) is None
    assert len(closed) == 1


def test_cancellation_after_reuse_closes_connection(tmp_path, monkeypatch):
    import threading

    directory, manifest, catalog, seal = prepared(tmp_path, monkeypatch)
    cancelled = threading.Event()

    def cancel(_artifact):
        cancelled.set()

    closed = replacing_validation(monkeypatch, directory / 'search.sqlite3', replace=cancel)
    with pytest.raises(TimeoutError, match='cancel'):
        query(catalog, manifest, seal, cancelled=cancelled.is_set)
    assert len(closed) == 1
