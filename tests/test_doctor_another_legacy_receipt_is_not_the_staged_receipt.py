"""A retained legacy attempt cannot borrow another receipt's authority."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import compile_memory as compiler
import doctor
import pytest
from markdown_transaction import MarkdownCoordinator
from transaction_lineage import committed_created_paths


@pytest.fixture
def race(tmp_path):
    fixture_path = Path(__file__).parent/'fixtures/compile-receipt-v3-foreign-body.json'
    fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
    assert fixture['encoding'] == 'base64+zlib'
    assert fixture['files'].keys() == fixture['sha256'].keys()
    for relative, encoded in fixture['files'].items():
        target = tmp_path/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_fixture_bytes(encoded, fixture['sha256'][relative]))
    return dict(fixture['metadata'], root=str(tmp_path/'vault'), state=str(tmp_path/'state'))


def _database(race):
    path = Path(race['state'])/'run/markdown-transactions.sqlite3'
    connection = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def test_a_foreign_committed_body_is_not_the_retained_staged_body(race):
    assert race['failed_operation'] != race['committed_operation']
    assert race['failed_after_hash'] != race['current_after_hash']
    assert race['original_sha256'] != race['current_original_sha256']
    database = _database(race)
    try:
        assert not doctor._compile_snapshot_was_written(
            database, race['failed_id'], Path(race['root']), Path(race['state'])
        )
    finally:
        database.close()


def test_generic_historical_outcome_is_distinct_from_current_context(race):
    root, state = Path(race['root']), Path(race['state'])
    database = _database(race)
    try:
        assert doctor._attempt_is_history(database, race['failed_id'], committed_created_paths(database),
                                         doctor._CompiledDaySupersession(root, state))
    finally:
        database.close()
    part = compiler._daily_parts('knowledge/daily/2026-07-14.md', (root/'knowledge/daily/2026-07-14.md').read_bytes())[-1]
    assert part.sha256 == race['part_sha256']
    coordinator = MarkdownCoordinator(root, state)
    assert not compiler._receipt_predicate(coordinator).matches(part)



def _fixture_bytes(encoded, digest):
    decoder = zlib.decompressobj()
    content = decoder.decompress(base64.b64decode(encoded, validate=True)) + decoder.flush()
    assert decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, 'incomplete or extra stream'
    assert hashlib.sha256(content).hexdigest() == digest, 'fixture content digest mismatch'
    return content


@pytest.mark.parametrize('packed', [
    b'not zlib',
    zlib.compress(b'original')[:-1],
    zlib.compress(b'original') + b'extra',
    zlib.compress(b'original') + zlib.compress(b'second'),
])
def test_fixture_rejects_damaged_or_extra_compressed_data(packed):
    encoded = base64.b64encode(packed).decode('ascii')
    with pytest.raises((zlib.error, AssertionError)):
        _fixture_bytes(encoded, hashlib.sha256(b'original').hexdigest())


def test_fixture_rejects_invalid_base64():
    with pytest.raises(binascii.Error):
        _fixture_bytes('!', hashlib.sha256(b'original').hexdigest())


def test_fixture_rejects_valid_stream_with_changed_content():
    encoded = base64.b64encode(zlib.compress(b'changed')).decode('ascii')
    with pytest.raises(AssertionError, match='digest mismatch'):
        _fixture_bytes(encoded, hashlib.sha256(b'original').hexdigest())
