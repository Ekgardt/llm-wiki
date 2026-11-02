"""Frozen bytes and expected records validated against pre-extraction 3e1def7e."""
import json
from pathlib import Path

import pytest
from llm_wiki.formats import receipts

FIXTURES = Path(__file__).with_name('fixtures')


def _parse(version, raw):
    expected = json.loads((FIXTURES / f'{version}.json').read_bytes())
    if version == 'v2':
        return receipts.parse_compile_receipt_v2(raw, expected['source_digest'])
    return receipts.parse_compile_receipt_version(
        raw, logical_path=expected['source']['logical_path'],
        source_sha256=expected['source']['sha256'],
    )


@pytest.mark.parametrize('version', ['v2', 'v3', 'v4'])
def test_frozen_receipts_preserve_complete_baseline_records(version):
    raw = (FIXTURES / f'{version}.md').read_bytes()
    expected = json.loads((FIXTURES / f'{version}.json').read_bytes())
    assert _parse(version, raw) == expected


@pytest.mark.parametrize('version', ['v2', 'v3', 'v4'])
@pytest.mark.parametrize('damage', ['utf8', 'body', 'json', 'identity'])
def test_frozen_receipt_corruption_is_rejected(version, damage):
    raw = (FIXTURES / f'{version}.md').read_bytes()
    mutations = {
        'utf8': raw.replace(b'# Compile', b'\xff Compile'),
        'body': raw.replace(b'# Compile', b'# Other'),
        'json': raw.replace(b'```json\n{', b'```json\n{,'),
        'identity': raw.replace(b'compile:', b'damaged:'),
    }
    with pytest.raises(ValueError):
        _parse(version, mutations[damage])


def test_unknown_version_remains_distinguishable_from_corruption():
    raw = (FIXTURES / 'v4.md').read_bytes().replace(b'compile-receipt/v4', b'compile-receipt/v99')
    with pytest.raises(receipts.UnsupportedCompileReceiptVersion):
        _parse('v4', raw)


@pytest.mark.parametrize('version', ['v3', 'v4'])
def test_receipt_cannot_be_reused_for_another_logical_path(version):
    raw = (FIXTURES / f'{version}.md').read_bytes()
    record = json.loads((FIXTURES / f'{version}.json').read_bytes())
    with pytest.raises(ValueError):
        receipts.parse_compile_receipt_version(
            raw, logical_path='knowledge/daily/other.md', source_sha256=record['source']['sha256'])
