"""Helpers shared by test_doctor_another_legacy_receipt_is_not_the_staged_receipt.py and the test files importing them."""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from pathlib import Path

import pytest


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


def _fixture_bytes(encoded, digest):
    decoder = zlib.decompressobj()
    content = decoder.decompress(base64.b64decode(encoded, validate=True)) + decoder.flush()
    assert decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, 'incomplete or extra stream'
    assert hashlib.sha256(content).hexdigest() == digest, 'fixture content digest mismatch'
    return content
