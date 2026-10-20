"""Recovery consumers do not retain a whole encoded image alongside plaintext."""
import lzma
from pathlib import Path

import markdown_transaction as transaction
import pytest
from reliable_memory import sha256_bytes

from tests.test_streamed_transaction_image_verification import _encoded, _image


def _forbid_eager(*args, **kwargs):
    raise AssertionError('complete encoded image materialized')


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
@pytest.mark.parametrize('payload', [b'', b'evidence\n' * 20000], ids=['empty', 'multiblock'])
def test_image_loading_does_not_read_an_encoded_buffer(tmp_path, monkeypatch, format, payload):
    artifact = _image(tmp_path, _encoded(payload, format))
    monkeypatch.setattr(Path, 'read_bytes', _forbid_eager)
    monkeypatch.setattr(lzma, 'decompress', _forbid_eager)
    assert transaction._image_bytes(artifact) == payload


@pytest.mark.parametrize('format', ['plain', 'xz', 'concatenated'])
def test_undo_descriptor_verification_does_not_materialize_plaintext(tmp_path, monkeypatch, format):
    payload = b'original evidence\n' * 20000
    artifact = _image(tmp_path, _encoded(payload, format))
    row = {'before_hash': sha256_bytes(payload), 'path': 'knowledge/notes/page.md', 'position': 0}
    monkeypatch.setattr(transaction, '_image_bytes', _forbid_eager)
    assert transaction._verified_before_image(artifact, row) == {
        'sha256': row['before_hash'], 'artifact': 'before/000000.bin'}


@pytest.mark.parametrize('reader', [transaction._image_bytes, transaction._image_digest])
def test_incomplete_concatenated_tail_is_not_a_successful_prefix(tmp_path, reader):
    artifact = _image(tmp_path, _encoded(b'first', 'xz') + _encoded(b'second', 'xz')[:-1])
    with pytest.raises(lzma.LZMAError):
        reader(artifact)


def test_wrong_before_hash_is_still_refused(tmp_path):
    artifact = _image(tmp_path, _encoded(b'actual bytes', 'xz'))
    row = {'before_hash': sha256_bytes(b'forged bytes'), 'path': 'knowledge/notes/page.md', 'position': 0}
    with pytest.raises(transaction.RefusedOperation, match='before-image is corrupt'):
        transaction._verified_before_image(artifact, row)


def test_apply_and_undo_use_file_decoder(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    target = root / 'knowledge/notes/page.md'
    target.parent.mkdir(parents=True)
    before = b'original\n' * 20000
    target.write_bytes(before)
    coordinator = transaction.MarkdownCoordinator(root, tmp_path / 'state')
    record = coordinator.prepare(
        [transaction.MarkdownChange.replace('knowledge/notes/page.md', b'replacement\n' * 20000)],
        operation_id='image-file-decoder')
    monkeypatch.setattr(lzma, 'decompress', _forbid_eager)
    assert coordinator.apply(record.id).state == 'committed'
    undo = coordinator.undo(record.id)
    assert coordinator.apply(undo.id).state == 'committed'
    assert target.read_bytes() == before
