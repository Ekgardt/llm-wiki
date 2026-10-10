"""Seed occurrences are membership, not evidence multiplicity."""
import hashlib
import time

import pytest
from evidence_graph import EvidenceGraph, create_generation_database
from retrieval import _neighbour_rows


def _source(name, content):
    return dict(source_id=name, relative_path=name + '.md', sha256=hashlib.sha256(content).hexdigest(), size=len(content), media_type='text/markdown', language=None, git_oid=None)


def _node(name):
    return dict(node_id=name, kind='concept', identity_scheme='test/v1', identity_key=name, metadata={})


def _occurrence(identity, node, source):
    return dict(occurrence_id=identity, node_id=node, source_id=source, role='mention', byte_start=0, byte_end=4, line_start=1, line_end=1)


def _assertion(identity, source, target, edge='LINKS_TO', resolution='resolved'):
    return dict(assertion_id=identity, source_node_id=source, target_node_id=target, edge_type=edge, literal=None, confidence='high', authority='ai-derived', resolution=resolution, extractor='test/v1')


def _evidence(identity, assertion):
    return dict(evidence_id=identity, assertion_id=assertion, observation_id=None, source_id='seed', byte_start=0, byte_end=4, span_sha256=hashlib.sha256(b'text').hexdigest())


def _graph(tmp_path, duplicates=2):
    content = b'text\n'
    sources = [_source(name, content) for name in ('seed', 'target', 'foreign')]
    occurrences = [_occurrence('seed-' + str(number), 'seed-node', 'seed') for number in range(duplicates)]
    occurrences += [_occurrence('target-a', 'target-node', 'target'), _occurrence('target-z', 'target-node', 'foreign'), _occurrence('foreign', 'foreign-node', 'foreign')]
    assertions = [_assertion('a', 'seed-node', 'target-node'), _assertion('b', 'seed-node', 'target-node', 'MENTIONS'), _assertion('foreign', 'foreign-node', 'target-node')]
    evidence = [_evidence('a-1', 'a'), _evidence('a-2', 'a'), _evidence('b-1', 'b'), _evidence('foreign-1', 'foreign')]
    path = tmp_path / 'graph.sqlite3'
    create_generation_database(path, sources=sources, source_bytes={name: content for name in ('seed', 'target', 'foreign')}, nodes=[_node(name) for name in ('seed-node', 'target-node', 'foreign-node')], occurrences=occurrences, assertions=assertions, evidence=evidence, observations=(), dependencies=())
    return EvidenceGraph(path, state_root=tmp_path)


def _rows(graph, path='seed.md', direction='out', edges=('LINKS_TO', 'MENTIONS'), limit=2, deadline=None):
    return _neighbour_rows(graph, seed_path=path, direction=direction, edges=edges, per_seed_limit=limit, deadline_monotonic=deadline)


def _identities(rows):
    return [(row['assertion_id'], row['evidence_id'], row['relative_path']) for row in rows]


def test_repeated_seed_occurrences_keep_each_distinct_evidence_once(tmp_path):
    with _graph(tmp_path) as graph:
        rows = _rows(graph)
    assert _identities(rows) == [('a', 'a-1', 'target.md'), ('a', 'a-2', 'target.md'), ('b', 'b-1', 'target.md')]


def test_repeated_seed_occurrences_do_not_exhaust_existing_row_ceiling(tmp_path):
    with _graph(tmp_path, duplicates=40) as graph:
        rows = _rows(graph, edges=('LINKS_TO',), limit=1)
    assert len(rows) == 2


def test_incoming_direction_preserves_all_distinct_assertions_and_evidence(tmp_path):
    with _graph(tmp_path) as graph:
        rows = _rows(graph, path='target.md', direction='in', edges=('LINKS_TO',))
    assert [(row['assertion_id'], row['evidence_id']) for row in rows] == [('foreign', 'foreign-1'), ('a', 'a-1'), ('a', 'a-2')]


def test_missing_seed_and_unmatched_edge_return_no_rows(tmp_path):
    with _graph(tmp_path) as graph:
        assert _rows(graph, path='missing.md') == []
        assert _rows(graph, edges=('DOCUMENTS',)) == []


def test_expired_caller_still_refuses_before_query(tmp_path):
    with _graph(tmp_path) as graph:
        with pytest.raises(TimeoutError):
            _rows(graph, deadline=time.monotonic() - 1)
