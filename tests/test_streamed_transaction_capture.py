"""Preparation owns a verified source instead of materializing before-bytes."""
import io

import markdown_transaction as transaction
import pytest


def _vault(tmp_path):
    root = tmp_path / 'vault'
    target = root / 'knowledge/notes/page.md'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'original evidence\n' * 10000)
    return transaction.MarkdownCoordinator(root, tmp_path / 'state'), target


def _forbid_eager(*args, **kwargs):
    raise AssertionError('complete before-image loaded into memory')


@pytest.mark.parametrize('kind', ['replace', 'append', 'delete'])
def test_prepare_does_not_materialize_before_bytes(tmp_path, monkeypatch, kind):
    coordinator, target = _vault(tmp_path)
    before = target.read_bytes()
    contents = {'replace': b'replacement', 'append': before + b'added\n', 'delete': None}
    change_kind = 'replace' if kind == 'append' else kind
    change = transaction.MarkdownChange(change_kind, 'knowledge/notes/page.md', contents[kind])
    with monkeypatch.context() as patch:
        patch.setattr(transaction, '_descriptor_bytes', _forbid_eager)
        record = coordinator.prepare([change], operation_id='streamed-' + kind)
    assert coordinator.apply(record.id).state == 'committed'
    undo = coordinator.undo(record.id)
    assert coordinator.apply(undo.id).state == 'committed'
    assert target.read_bytes() == before


def test_change_during_image_creation_is_refused_before_prepare(tmp_path, monkeypatch):
    coordinator, target = _vault(tmp_path)
    write = coordinator._write_compressed_image

    def replace_source(path, content):
        write(path, content)
        target.write_bytes(b'changed during capture')

    monkeypatch.setattr(coordinator, '_write_compressed_image', replace_source)
    with pytest.raises(ValueError, match='changed'):
        coordinator.prepare(
            [transaction.MarkdownChange.delete('knowledge/notes/page.md')],
            operation_id='changed-capture')
    assert coordinator._record_for_operation_id('changed-capture') is None
    assert not list(coordinator.transaction_root.glob('*/before/*.bin'))


def test_capture_range_cannot_escape_its_owner(tmp_path):
    coordinator, target = _vault(tmp_path)
    with coordinator._capture_target(target) as (source, identity):
        assert source[:8] == b'original'
        assert identity == coordinator._parent_identity(target.parent)
    with pytest.raises(ValueError, match='closed'):
        source.range_digest(0, len(source))


def test_absent_target_created_during_staging_is_refused(tmp_path, monkeypatch):
    coordinator, target = _vault(tmp_path)
    target.unlink()
    write = coordinator._write_compressed_image

    def create_source(path, content):
        write(path, content)
        target.write_bytes(b'external creation')

    monkeypatch.setattr(coordinator, '_write_compressed_image', create_source)
    with pytest.raises(ValueError, match='changed'):
        coordinator.prepare(
            [transaction.MarkdownChange.create('knowledge/notes/page.md', b'new')],
            operation_id='created-during-staging')
    assert target.read_bytes() == b'external creation'
    assert coordinator._record_for_operation_id('created-during-staging') is None


@pytest.mark.skipif(not transaction._use_posix_dir_fd(), reason='POSIX parent replacement')
def test_parent_replaced_during_staging_is_refused(tmp_path, monkeypatch):
    coordinator, target = _vault(tmp_path)
    write = coordinator._write_compressed_image

    def replace_parent(path, content):
        write(path, content)
        target.parent.rename(target.parent.with_name('original-notes'))
        target.parent.mkdir()
        target.write_bytes(b'external replacement')

    monkeypatch.setattr(coordinator, '_write_compressed_image', replace_parent)
    with pytest.raises(transaction.TargetBoundaryFailure, match='parent identity'):
        coordinator.prepare(
            [transaction.MarkdownChange.delete('knowledge/notes/page.md')],
            operation_id='parent-replaced-during-staging')
    assert target.read_bytes() == b'external replacement'
    assert coordinator._record_for_operation_id('parent-replaced-during-staging') is None



def test_final_content_check_preserves_range_cursor():
    source = transaction.VerifiedByteRange(io.BytesIO(b'abcdef'), 6, None, chunk_size=2)
    assert source[:2] == b'ab'
    source.verify_content()
    assert source[2:] == b'cdef'


def test_final_content_check_rejects_changes_hidden_by_cached_blocks():
    stream = io.BytesIO(b'abcdef')
    source = transaction.VerifiedByteRange(stream, 6, None, chunk_size=2)
    assert source[:2] == b'ab'
    stream.seek(0)
    stream.write(b'XX')
    with pytest.raises(PermissionError, match='content changed'):
        source.verify_content()
