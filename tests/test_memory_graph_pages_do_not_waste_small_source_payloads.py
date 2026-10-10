"""Small captured sources must not each pay for a mostly empty overflow page."""
import hashlib
import sqlite3
from contextlib import closing

import evidence_graph as graph
import evidence_graph_builder as builder
import pytest
from generation_catalog import GenerationCatalog


def _records():
    sources, content = [], {}
    for index in range(400):
        identifier = f'source:session-{index:04d}'
        body = (f'# Captured event {index}\n' + 'evidence ' * 140).encode()
        sources.append(dict(source_id=identifier,
                            relative_path=f'knowledge/raw/sessions/day/event-{index:04d}.md',
                            sha256=hashlib.sha256(body).hexdigest(), size=len(body),
                            media_type='text/markdown', language=None, git_oid=None))
        content[identifier] = body
    return dict(sources=sources, source_bytes=content, nodes=[], occurrences=[],
                assertions=[], evidence=[], observations=[], dependencies=[])


def _all_sources(path):
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as database:
        return list(database.execute('select * from source order by source_id'))


def test_memory_builder_reduces_overflow_waste_without_losing_sources(tmp_path):
    records = _records()
    baseline = tmp_path / 'ordinary-default.sqlite3'
    graph.create_generation_database(baseline, **records)
    catalog = GenerationCatalog(tmp_path / 'state')
    built = builder.build_full_generation(catalog, generation_id='memory-pages',
                                         activate=False, **records)
    candidate = built.generation_path / 'evidence.sqlite3'
    assert candidate.stat().st_size < baseline.stat().st_size
    assert _all_sources(candidate) == _all_sources(baseline)
    graph.validate_generation_database(candidate, schema=graph.GraphSchema.V2)


def test_code_builder_preserves_existing_page_storage(tmp_path):
    records = _records()
    baseline = tmp_path / 'ordinary-default.sqlite3'
    graph.create_generation_database(baseline, **records)
    catalog = GenerationCatalog(tmp_path / 'state')
    built = builder.build_full_generation(catalog, generation_id='code-pages',
                                         activate=False, policy={'code_roots': ['src']}, **records)
    candidate = built.generation_path / 'evidence.sqlite3'
    assert candidate.stat().st_size == baseline.stat().st_size
    assert _all_sources(candidate) == _all_sources(baseline)
    graph.validate_generation_database(candidate, schema=graph.GraphSchema.V2)


def _empty_records():
    return dict(sources=[], source_bytes={}, nodes=[], occurrences=[], assertions=[],
                evidence=[], observations=[], dependencies=[])


@pytest.mark.parametrize('page_size', [512, 1024, 2048, 4096, 8192, 16384, 32768, 65536])
def test_all_sqlite_page_sizes_preserve_the_exact_schema(tmp_path, page_size):
    path = tmp_path / 'supported.sqlite3'
    graph.create_generation_database(path, page_size=page_size, **_empty_records())
    graph.validate_generation_database(path, schema=graph.GraphSchema.V2)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute('pragma page_size').fetchone()[0] == page_size


@pytest.mark.parametrize('page_size', [4097, 0, -8192, 8192.0, True, '8192'])
def test_unsupported_page_sizes_fail_without_leaving_a_database(tmp_path, page_size):
    path = tmp_path / 'unsupported.sqlite3'
    with pytest.raises((TypeError, ValueError), match='page size'):
        graph.create_generation_database(path, page_size=page_size, **_empty_records())
    assert list(tmp_path.iterdir()) == []
