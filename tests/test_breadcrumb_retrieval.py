"""Only complete linked breadcrumb evidence joins the authoritative corpus."""
from __future__ import annotations

import breadcrumb_evidence
import corpus_snapshot
import pytest

from tests.test_breadcrumb_evidence import _disk_source, _source


def test_large_event_parts_enter_the_corpus_with_exact_physical_spans(tmp_path):
    content = b'beginmarker ' + b'ordinary ' * 130000 + b' endmarker'
    _disk_source(tmp_path, content)
    snapshot = corpus_snapshot.collect_corpus(tmp_path)
    by_path = {source.record.relative_path: source.content for source in snapshot.sources}
    text = b''.join(by_path.values())
    assert b'beginmarker' in text and b'endmarker' in text
    assert snapshot.chunks
    assert all(chunk.text.encode() == by_path[chunk.source_path][chunk.byte_start:chunk.byte_end]
               for chunk in snapshot.chunks)


def test_unlinked_parts_and_ordinary_sessions_do_not_enter_the_corpus(tmp_path):
    head, documents = _source(b'not yet complete')
    documents.pop(head)
    documents['knowledge/raw/sessions/2026-09-29/ordinary.md'] = b'# Ordinary session\n'
    for relative, content in documents.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    assert corpus_snapshot.collect_corpus(tmp_path).sources == ()


@pytest.mark.parametrize('mutation', ['missing', 'changed'])
def test_corrupt_linked_parts_refuse_the_snapshot(tmp_path, mutation):
    _disk_source(tmp_path, b'complete event')
    part = next(tmp_path.rglob('*.breadcrumb-part.md'))
    actions = {'missing': part.unlink, 'changed': lambda: part.write_bytes(b'changed')}
    actions[mutation]()
    with pytest.raises((OSError, ValueError)):
        corpus_snapshot.collect_corpus(tmp_path)


def test_archived_breadcrumbs_follow_the_existing_historical_policy(tmp_path):
    head = _disk_source(tmp_path, b'archivedmarker', archived=True)
    assert corpus_snapshot.collect_corpus(tmp_path).sources == ()
    historical = corpus_snapshot.collect_corpus(tmp_path, include_historical=True)
    assert breadcrumb_evidence.archived_source_path(head) in {
        source.record.relative_path for source in historical.sources
    }


def test_breadcrumb_sources_keep_their_verified_occurrence_day(tmp_path):
    _disk_source(tmp_path, b'dated evidence')
    sources = corpus_snapshot.collect_corpus(tmp_path).sources
    assert sources
    assert {source.metadata.valid_from for source in sources} == {'2026-09-29'}


def test_code_collection_does_not_import_private_breadcrumbs(tmp_path):
    _disk_source(tmp_path, b'private event')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/a.py').write_text('VALUE = 1\n')
    snapshot = corpus_snapshot.collect_corpus(tmp_path, code_roots=('scripts',))
    assert [source.record.relative_path for source in snapshot.sources] == ['scripts/a.py']


def _indexed_corpus(tmp_path, monkeypatch, embedder=None):
    import search_memory
    from generation_catalog import GenerationCatalog

    from tests.test_generation_catalog import _initialize_repository
    from tests.test_search_ranking import _activate_search_generation

    _initialize_repository(tmp_path)
    snapshot = corpus_snapshot.collect_corpus(tmp_path)
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    catalog = GenerationCatalog(tmp_path / 'state')
    directory = catalog.generations_path / 'gen-search'
    directory.mkdir()
    descriptors = [search_memory.build_generation_fts(snapshot, directory)]
    vector = None
    if embedder is not None:
        vector = {'model_id': 'unit/source-admission', 'model_revision': '1', 'dimensions': 2}
        descriptors.extend(search_memory.build_generation_numpy_vectors(
            snapshot, directory, embedder=embedder, **vector,
        ))
    catalog, _manifest = _activate_search_generation(tmp_path, snapshot, descriptors, vector=vector)
    return snapshot, catalog


def test_public_retrieval_delivers_a_large_event_tail_as_verifiable_evidence(tmp_path, monkeypatch):
    import hashlib

    from query_memory import build_grounded_context
    from retrieval import retrieve_via_search_memory

    _disk_source(tmp_path, b'ordinary ' * 130000 + b' secretendmarker')
    snapshot, catalog = _indexed_corpus(tmp_path, monkeypatch)
    rows = retrieve_via_search_memory(
        'secretendmarker', catalog=catalog, semantic=False, graph=False,
        rerank=False, emit_telemetry=False,
    )
    context = build_grounded_context(snapshot, rows, vault=tmp_path, profile='BASE')
    assert 'secretendmarker' in context.prompt_context
    assert context.evidence
    for evidence in context.evidence:
        original = (tmp_path / evidence.relative_path).read_bytes()
        assert hashlib.sha256(original[evidence.byte_start:evidence.byte_end]).hexdigest() == evidence.span_sha256


def test_without_a_generation_the_large_event_tail_is_still_readable(tmp_path, monkeypatch):
    import search_memory
    from query_memory import build_grounded_context

    _disk_source(tmp_path, b'ordinary ' * 130000 + b' fallbackendmarker')
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    rows = search_memory.markdown_hits('fallbackendmarker')
    assert rows and all(row['fallback_reason'] == 'no_active_generation' for row in rows)
    context = build_grounded_context(corpus_snapshot.collect_corpus(tmp_path), rows,
                                     vault=tmp_path, profile='BASE')
    assert 'fallbackendmarker' in context.prompt_context
    assert len(context.prompt_context.encode()) < len(next(tmp_path.rglob('*.breadcrumb-part.md')).read_bytes())


def test_notes_only_fallback_does_not_read_corrupt_private_events(tmp_path, monkeypatch):
    import search_memory

    _disk_source(tmp_path, b'private marker')
    next(tmp_path.rglob('*.breadcrumb-part.md')).unlink()
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    assert search_memory.markdown_hits('marker', scope='wiki') == []


def test_fallback_refuses_incomplete_evidence(tmp_path, monkeypatch):
    import search_memory

    _disk_source(tmp_path, b'private marker')
    next(tmp_path.rglob('*.breadcrumb-part.md')).unlink()
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    with pytest.raises((OSError, ValueError)):
        search_memory.markdown_hits('marker')


def test_fallback_respects_an_already_elapsed_deadline(tmp_path, monkeypatch):
    import time

    import search_memory

    _disk_source(tmp_path, b'private marker')
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    with pytest.raises(TimeoutError):
        search_memory.markdown_hits('marker', deadline=time.monotonic() - 1)


def _write_numbered_event(root, number, text):
    import hashlib
    from datetime import datetime, timezone

    import breadcrumb_protocol as protocol

    content = text.encode()
    identity = hashlib.sha256(str(number).encode()).hexdigest()
    moment = datetime(2026, 9, 29, tzinfo=timezone.utc)
    anchor = protocol.make_anchor(identity, content, occurred_at=moment,
                                  accepted_at=moment, time_origin='host')
    parts = protocol.encode_parts(identity, content)
    manifest = protocol.make_manifest(anchor, parts)
    for relative, data in breadcrumb_evidence.source_documents(manifest, anchor, parts):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_raw_events_cannot_displace_a_claim_before_candidate_admission(tmp_path, monkeypatch):
    from retrieval import retrieve_via_search_memory

    note = tmp_path / 'knowledge/notes/accepted-policy.md'
    note.parent.mkdir(parents=True)
    note.write_text('---\ntype: decision\nsource_authority: user\n---\n# Accepted policy\n'
                    + 'background ' * 1000 + ' rarepolicy keeps the original evidence.\n')
    # Six raw matches exceed the old one-row query's five-row lexical overfetch.
    for number in range(6):
        _write_numbered_event(tmp_path, number, 'rarepolicy ' * 20)
    _snapshot, catalog = _indexed_corpus(tmp_path, monkeypatch)
    rows = retrieve_via_search_memory(
        'rarepolicy', catalog=catalog, semantic=False, graph=False,
        rerank=False, emit_telemetry=False, limit=1, max_candidates=1,
    )
    assert rows[0]['path'] == 'knowledge/notes/accepted-policy.md'


def _unit_vectors(texts):
    import numpy as np

    # Controlled vectors isolate admission/ranking; these are not quality scores.
    return np.array([[0.1, 0.99] if 'background' in text else [1.0, 0.0]
                     for text in texts], dtype=np.float32)


def test_dense_admission_retains_claims_without_overriding_the_scored_order(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    import search_memory

    note = tmp_path / 'knowledge/notes/accepted-policy.md'
    note.parent.mkdir(parents=True)
    note.write_text('---\ntype: decision\nsource_authority: user\n---\n# Policy\nbackground policy')
    for number in range(4):
        _write_numbered_event(tmp_path, number, 'rarepolicy')
    _snapshot, catalog = _indexed_corpus(tmp_path, monkeypatch, _unit_vectors)
    directory = catalog.generations_path / 'gen-search'
    with closing(sqlite3.connect(directory / search_memory.GENERATION_FTS_ARTIFACT)) as connection:
        connection.row_factory = sqlite3.Row
        rows = search_memory._generation_vector_rows(
            'rarepolicy', connection, catalog.get_active(), directory, 'gen-search',
            embedder=_unit_vectors, model_id='unit/source-admission', model_revision='1',
            scope='all', limit=1, project=None, since=None, as_of=None,
            deadline=None, cancelled=None,
        )
    assert any(row['path'] == 'knowledge/notes/accepted-policy.md' for row in rows)
    assert [row['score'] for row in rows] == sorted((row['score'] for row in rows), reverse=True)
