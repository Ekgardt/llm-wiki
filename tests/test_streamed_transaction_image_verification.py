"""Full plaintext image integrity without retaining the decoded image."""
from __future__ import annotations

import lzma

import pytest
from llm_wiki import markdown_prepare
from reliable_memory import sha256_bytes

from tests.markdown_coordinator_parts import patch_coordinator
from tests.streamed_transaction_image_verification_helpers import _encoded, _image


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
@pytest.mark.parametrize('payload', [b'', b'retained evidence\n' * 1000], ids=['empty', 'evidence'])
def test_stored_image_verification_does_not_materialize_plaintext(tmp_path, monkeypatch, format, payload):
    _image(tmp_path, _encoded(payload, format))
    patch_coordinator(monkeypatch, 'image_bytes', _forbid_full_image)
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(payload)}
    markdown_prepare.verify_stored_image(state, 'before', tmp_path)


def _forbid_full_image(*args):
    raise AssertionError('verification materialized the complete image')


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
def test_image_verification_still_refuses_wrong_plaintext_hash(tmp_path, format):
    _image(tmp_path, _encoded(b'actual evidence', format))
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(b'forged evidence')}
    with pytest.raises(RuntimeError, match='artifact hash mismatch'):
        markdown_prepare.verify_stored_image(state, 'before', tmp_path)


def test_truncated_image_still_fails_with_decompression_error(tmp_path):
    payload = b'retained evidence' * 1000
    _image(tmp_path, lzma.compress(payload, preset=markdown_prepare.IMAGE_COMPRESSION_PRESET)[:-1])
    state = {'artifact': 'before/000000.bin', 'sha256': sha256_bytes(payload)}
    with pytest.raises(lzma.LZMAError):
        markdown_prepare.verify_stored_image(state, 'before', tmp_path)
