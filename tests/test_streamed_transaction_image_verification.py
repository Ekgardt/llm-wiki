"""Full plaintext image integrity without retaining the decoded image."""
from __future__ import annotations

import lzma

import markdown_transaction as transaction
import pytest
from reliable_memory import sha256_bytes


def _encoded(payload, format):
    if format == 'plain':
        return payload
    if format == 'xz':
        return transaction._compressed_image(payload)
    middle = len(payload) // 2
    return transaction._compressed_image(payload[:middle]) + transaction._compressed_image(payload[middle:])


def _image(root, raw):
    path = root / 'before/000000.bin'
    path.parent.mkdir()
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
@pytest.mark.parametrize('payload', [b'', b'retained evidence\n' * 1000], ids=['empty', 'evidence'])
def test_stored_image_verification_does_not_materialize_plaintext(tmp_path, monkeypatch, format, payload):
    _image(tmp_path, _encoded(payload, format))
    monkeypatch.setattr(transaction, '_image_bytes', _forbid_full_image)
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(payload)}
    transaction._verify_stored_image(state, 'before', tmp_path)


def _forbid_full_image(*args):
    raise AssertionError('verification materialized the complete image')


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
def test_image_verification_still_refuses_wrong_plaintext_hash(tmp_path, format):
    _image(tmp_path, _encoded(b'actual evidence', format))
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(b'forged evidence')}
    with pytest.raises(RuntimeError, match='artifact hash mismatch'):
        transaction._verify_stored_image(state, 'before', tmp_path)


def test_truncated_image_still_fails_with_decompression_error(tmp_path):
    payload = b'retained evidence' * 1000
    _image(tmp_path, transaction._compressed_image(payload)[:-1])
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(payload)}
    with pytest.raises(lzma.LZMAError):
        transaction._verify_stored_image(state, 'before', tmp_path)
