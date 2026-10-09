"""Weighted admission must survive; admitted relevance reaches rank fusion before its final prior."""
import sqlite3

import pytest
import retrieval
import search_memory

from tests.test_dense_admission_trust_weight import _COLUMNS, _COMMENTARY, _DECISION, _row


def _paths(rows):
    return [row['path'] for row in rows]


def _database():
    handle = sqlite3.connect(':memory:')
    handle.row_factory = sqlite3.Row
    handle.execute(f'CREATE TABLE chunks ({_COLUMNS})')
    handle.executemany(f"INSERT INTO chunks VALUES ({','.join('?' * 18)})",
                       [_row(0, _COMMENTARY, 'doc'), _row(1, _DECISION, 'decision')])
    handle.executemany('UPDATE chunks SET rank=? WHERE chunk_order=?', [(-0.875, 0), (-0.828, 1)])
    return handle


def test_dense_preserves_admission_but_fuses_the_original_relevance_order():
    with _database() as database:
        rows = search_memory._vector_scored_rows(database, [0.875, 0.828], 'fixture-generation',
            scope='all', since=None, as_of=None, project=None, deadline=None, cancelled=None, limit=1)
        assert _paths(rows) == [_DECISION]
        rows = search_memory._vector_scored_rows(database, [0.875, 0.828], 'fixture-generation',
            scope='all', since=None, as_of=None, project=None, deadline=None, cancelled=None, limit=2)
    assert _paths(rows) == [_DECISION, _COMMENTARY]
    fused_input = retrieval._dense_filtered_hits(rows, {'scope': 'all'})
    assert _paths(fused_input) == [_COMMENTARY, _DECISION]
    assert [row['vector_score'] for row in fused_input] == [0.875, 0.828]
    assert all('_signal_score' not in row for row in fused_input)


def test_lexical_keeps_its_standalone_prior_and_relevance_boosts():
    with _database() as database:
        source_rows = database.execute('SELECT * FROM chunks ORDER BY chunk_order').fetchall()
        rows = [search_memory._generation_result(row, 'fixture-generation') for row in source_rows]
    search_memory._boost_generation_results(rows, 'unmatched', None, deadline=None, cancelled=None)
    rows.sort(key=lambda row: -row['score'])
    assert _paths(rows) == [_DECISION, _COMMENTARY]
    fused_input = retrieval._filtered_hits(rows, {'scope': 'all'})
    assert _paths(fused_input) == [_COMMENTARY, _DECISION]
    assert [row['score'] for row in fused_input] == [0.875, 0.828]


def test_explicit_external_ranked_lists_keep_their_order():
    rows = [{'path': 'first.md', 'score': 0.2, 'source_sha256': 'a' * 64},
            {'path': 'second.md', 'score': 0.9, 'source_sha256': 'b' * 64}]
    assert _paths(retrieval._filtered_hits(rows, {'scope': 'all'})) == ['first.md', 'second.md']


def test_final_prior_is_applied_to_raw_rrf_once(monkeypatch):
    monkeypatch.setattr(retrieval, '_standing_disposition', lambda query: {})
    monkeypatch.setattr(retrieval, '_neighbour_boosts', lambda scores, meta: {})
    row = {'candidate_id': 'supported', 'path': _DECISION, 'score': 0.828,
           'type': 'decision', 'authority': 'ai-derived', 'content': 'body text', 'source_sha256': 'a' * 64}
    candidates, meta = retrieval.fuse_rrf(lexical=[row], dense=[row], graph=None, intents=('question',))
    candidate = candidates[0]
    assert candidate.final_score == pytest.approx(candidate.rrf_score * 1.25, abs=1e-6)
    assert meta['supported']['authority_weight'] == 1.0
    assert meta['supported']['type_weight'] == 1.25


def test_relevance_companion_pool_does_not_evict_the_curated_pool():
    with _database() as database:
        rows = search_memory._vector_scored_rows(database, [0.875, 0.828], 'fixture-generation',
            scope='all', since=None, as_of=None, project=None, deadline=None, cancelled=None,
            limit=1, include_unweighted=True)
    assert set(_paths(rows)) == {_COMMENTARY, _DECISION}
    assert len(rows) == 2
    assert [row['path'] for row in retrieval._dense_filtered_hits(rows, {'scope': 'all'})] == [_COMMENTARY, _DECISION]


def test_markdown_fallback_preserves_both_existing_and_relevance_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(search_memory, 'ROOT', tmp_path)
    notes = tmp_path / 'knowledge/notes'
    notes.mkdir(parents=True)
    useful = notes / 'useful.md'
    useful.write_text('---\ntype: debugging\nsource_authority: inferred\n---\n# Detail\nneedle context\n')
    decisions = [notes / f'decision-{number}.md' for number in range(3)]
    for path in decisions:
        path.write_text('---\ntype: decision\nsource_authority: user\n---\n# Status\nneedle\n')
    pages = [useful, *decisions]
    original = search_memory._direct_markdown_hits('needle context', pages, limit=1,
        project=None, since=None, as_of=None, deadline=None, cancelled=None)
    assert set(_paths(original)) == {str(path.relative_to(tmp_path)) for path in decisions}
    extended = search_memory._direct_markdown_hits('needle context', pages, limit=1,
        project=None, since=None, as_of=None, deadline=None, cancelled=None, include_unweighted=True)
    assert set(_paths(original)).issubset(_paths(extended))
    assert 'knowledge/notes/useful.md' in _paths(extended)
    assert len(extended) <= 2 * len(original)
