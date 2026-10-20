"""Create the same durable XZ image without a complete compressed buffer."""
from __future__ import annotations

import lzma

import markdown_transaction as transaction
import pytest
from reliable_memory import sha256_bytes


def _stage(coordinator, root, payload, mode):
    if mode == 'append':
        return coordinator._stage_append(root, 0, b'previous\n', b'previous\n' + payload)[1]
    return coordinator._stage_state(root, 0, payload)


@pytest.mark.parametrize('mode', ['full', 'append'])
def test_image_creation_does_not_build_a_complete_compressed_buffer(tmp_path, monkeypatch, mode):
    root = tmp_path / 'after'
    root.mkdir()
    payload = b'new durable evidence\n' * 10000
    expected = lzma.compress(payload, preset=transaction.IMAGE_COMPRESSION_PRESET)
    coordinator = object.__new__(transaction.MarkdownCoordinator)
    monkeypatch.setattr(lzma, 'compress', _forbid_complete_compression)
    state = _stage(coordinator, root, payload, mode)
    assert (root / '000000.bin').read_bytes() == expected
    assert transaction._image_digest(root / '000000.bin') == sha256_bytes(payload)
    assert state.get('suffix_sha256', state['sha256']) == sha256_bytes(payload)


def _forbid_complete_compression(*args, **kwargs):
    raise AssertionError('staging materialized the complete compressed image')


def test_image_footer_is_complete_before_fsync(tmp_path, monkeypatch):
    root = tmp_path / 'after'
    root.mkdir()
    payload = b'complete image before flush\n' * 1000
    observed = []
    original = transaction.fsync_file

    def sync(path):
        assert lzma.decompress(path.read_bytes()) == payload
        observed.append(path)
        original(path)

    monkeypatch.setattr(transaction, 'fsync_file', sync)
    object.__new__(transaction.MarkdownCoordinator)._stage_state(root, 0, payload)
    assert observed == [root / '000000.bin']


def test_interrupted_image_creation_does_not_publish_or_leave_prepared_work(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    (root / 'knowledge/notes').mkdir(parents=True)
    coordinator = transaction.MarkdownCoordinator(root, tmp_path / 'state')
    original = lzma.LZMAFile.write

    def interrupted(self, data):
        original(self, data)
        raise OSError('compression output interrupted')

    monkeypatch.setattr(lzma.LZMAFile, 'write', interrupted)
    with pytest.raises(OSError, match='compression output interrupted'):
        coordinator.prepare(
            [transaction.MarkdownChange.create('knowledge/notes/new.md', b'evidence\n' * 10000)],
            operation_id='streamed-image-failure')
    assert not (root / 'knowledge/notes/new.md').exists()
    assert coordinator._record_for_operation_id('streamed-image-failure') is None
    assert not list(coordinator.transaction_root.glob('*/after/*.bin'))
