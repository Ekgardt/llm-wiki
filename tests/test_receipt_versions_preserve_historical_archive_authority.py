from __future__ import annotations

import json
from pathlib import Path

import compile_memory as compiler
import pytest
from archive_daily import DailyArchiver
from evidence_resolver import (
    EvidenceResolutionError,
    _require_archive_receipt_context,
    validate_bag,
)

from tests.test_archive_daily_bagit import _archiver
from tests.test_archive_daily_bagit import archive_vault as archive_vault


def _restore_v3_bag(directory):
    fixture = Path(__file__).parent / 'fixtures/compile-receipt-v3-bag.json'
    files = json.loads(fixture.read_bytes())['files']
    directory.mkdir()
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    DailyArchiver._seal(directory)
    return directory


def _receipt(path):
    raw = (path / 'compile-receipt.md').read_bytes()
    logical, digest = compiler._receipt_source_fields(raw)
    return compiler.parse_compile_receipt_version(raw, logical_path=logical, source_sha256=digest)


def test_actual_historical_v3_and_new_v4_bags_both_validate(archive_vault, tmp_path):
    root, state, daily = archive_vault
    old = _restore_v3_bag(tmp_path / 'historical-v3')
    new = _archiver(root, state).archive(daily.stem).bag_path
    assert validate_bag(old).payload
    assert validate_bag(new).payload
    assert _receipt(old)['schema_version'] == 'compile-receipt/v3'
    assert _receipt(new)['schema_version'] == 'compile-receipt/v4'


@pytest.mark.parametrize('field,value', [('original_sha256', '0'*64), ('original_byte_size', 999999), ('byte_start', 1)])
def test_v4_archive_context_proof_rejects_forged_parent_or_span(archive_vault, field, value):
    root, state, daily = archive_vault
    archived = _archiver(root, state).archive(daily.stem).bag_path
    validated = validate_bag(archived)
    receipt = _receipt(archived)
    receipt['source'][field] = value
    with pytest.raises(EvidenceResolutionError, match='original context'):
        _require_archive_receipt_context(receipt, validated.payload, 0, len(validated.payload))


def test_one_archive_operation_discovers_its_receipts_only_once(archive_vault, monkeypatch):
    root, state, daily = archive_vault
    discovered = []
    original = compiler._v4_receipt_paths

    def track(directory, active):
        discovered.append(directory)
        yield from original(directory, active)

    monkeypatch.setattr(compiler, '_v4_receipt_paths', track)
    archiver = _archiver(root, state)
    assert archiver.eligible(daily).eligible
    assert archiver.eligible(daily).eligible
    assert archiver.archive(daily.stem).bag_path.exists()
    assert discovered == [root / 'knowledge/daily/receipts']


def _range_receipt(payload):
    from reliable_memory import sha256_bytes

    return {'schema_version': 'compile-receipt/v4', 'source': {
        'original_byte_size': len(payload), 'original_sha256': sha256_bytes(payload),
        'byte_start': 0, 'byte_end': len(payload)}}


def test_streamed_receipt_context_does_not_materialize_payload(monkeypatch):
    import io

    from bounded_io import VerifiedByteRange

    payload = b'# day\n## [evt-1] event\nA fact.\n'
    source = VerifiedByteRange(io.BytesIO(payload), len(payload), None)
    receipt = _range_receipt(payload)
    monkeypatch.setattr(VerifiedByteRange, '__getitem__', _refuse_materialized_range)
    _require_archive_receipt_context(receipt, source, 0, len(source))


def _refuse_materialized_range(*args):
    raise AssertionError('validation copied a source range')


@pytest.mark.parametrize('field,value', [
    ('original_sha256', '0' * 64), ('original_byte_size', 999999), ('byte_start', 1)])
def test_streamed_receipt_context_preserves_forgery_checks(field, value):
    import io

    from bounded_io import VerifiedByteRange

    payload = b'# day\n## [evt-1] event\nA fact.\n'
    source = VerifiedByteRange(io.BytesIO(payload), len(payload), None)
    receipt = _range_receipt(payload)
    receipt['source'][field] = value
    with pytest.raises(EvidenceResolutionError, match='original context'):
        _require_archive_receipt_context(receipt, source, 0, len(source))


@pytest.mark.parametrize('start,end', [
    (0, 1), (2, 3), (3, 7), (0, 8), (7, 8), (0, 0), (-2, -1), (100, 100)])
def test_streamed_line_span_uses_offsets_without_prefix_copies(monkeypatch, start, end):
    import io

    from bounded_io import VerifiedByteRange
    from evidence_resolver import _line_span

    payload = b'a\nb\nxyz\n'
    source = VerifiedByteRange(io.BytesIO(payload), len(payload), None)
    expected = _line_span(payload, start, end)
    monkeypatch.setattr(VerifiedByteRange, '__getitem__', _refuse_materialized_range)
    assert _line_span(source, start, end) == expected
