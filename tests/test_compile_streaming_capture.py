"""Source capture must not retain completed files while emitting the next one."""
import hashlib
import io
import weakref
import zipfile

import compile_controller as controller
import compile_memory as compiler
import pytest

from tests.test_compile_transactions import vault as vault


def _daily(root, name='2026-10-01'):
    path = root / 'knowledge/daily' / (name + '.md')
    path.write_bytes(b'# Daily\n\n## [10:00:00] session-end | manual\nKeep complete evidence.\n')
    return path


def test_stream_capture_matches_existing_complete_snapshot(vault):
    root, _ = vault
    path = _daily(root)
    (root / 'knowledge/notes/last.md').write_bytes(b'---\ntype: concept\n---\nLast page.\n')
    expected = compiler.snapshot_compile_inputs([path])
    output = io.BytesIO()
    digest = controller.write_compile_capture(output, [path])
    assert digest == controller.compile_inputs_digest(expected)
    with zipfile.ZipFile(output) as archive:
        assert controller.CompileInputArchive(archive, digest).materialize() == expected


def test_stream_capture_releases_completed_source_records(vault):
    root, _ = vault
    paths = [_daily(root, name) for name in ('2026-10-01', '2026-10-02')]
    observed = []
    for group, record in compiler.iter_compile_records(paths):
        if group == 'sources':
            observed.append(weakref.ref(record))
    del record
    assert observed
    assert all(reference() is None for reference in observed)


def test_stream_capture_writes_before_reading_next_daily(vault, monkeypatch):
    root, _ = vault
    paths = [_daily(root, name) for name in ('2026-10-01', '2026-10-02')]
    output = io.BytesIO()
    original = compiler._read_daily_source

    def read(path):
        if path == paths[1]:
            assert output.tell() > 0
        return original(path)

    monkeypatch.setattr(compiler, '_read_daily_source', read)
    digest = controller.write_compile_capture(output, paths)
    assert len(digest) == hashlib.sha256().digest_size * 2


def test_stream_capture_keeps_receipt_disposition(vault):
    root, _ = vault
    path = _daily(root)
    output = io.BytesIO()
    digest = controller.write_compile_capture(output, [path], compiled=lambda logical, digest: True)
    with zipfile.ZipFile(output) as archive:
        reader = controller.CompileInputArchive(archive, digest)
        assert reader.record_count('dailies') == 0
        assert any(source.logical_path == compiler._logical_path(path)
                   for source in reader.records('sources'))


def test_stream_capture_propagates_late_source_failure(vault, monkeypatch):
    root, _ = vault
    path = _daily(root)

    def refuse(source, **options):
        raise OSError('source became unavailable')

    monkeypatch.setattr(compiler, '_snapshot', refuse)
    with pytest.raises(OSError, match='source became unavailable'):
        controller.write_compile_capture(io.BytesIO(), [path])


def test_stream_capture_releases_previous_daily_before_next_source(vault):
    root, _ = vault
    paths = [_daily(root, name) for name in ('2026-10-01', '2026-10-02', '2026-10-03')]
    observed = []
    for group, record in compiler.iter_compile_records(paths):
        if _is_daily_source(group, record):
            assert all(reference() is None for reference in observed)
            observed.append(weakref.ref(record))


def test_durable_writer_does_not_publish_incomplete_capture(vault, monkeypatch, tmp_path):
    import memory_queue

    root, _ = vault
    path = _daily(root)
    target = tmp_path / 'snapshot.result'

    def refuse(*args, **options):
        raise OSError('late source failure')

    monkeypatch.setattr(compiler, '_snapshot', refuse)
    with pytest.raises(OSError, match='late source failure'):
        memory_queue._write_durable_producer(target, lambda output: controller.write_compile_capture(output, [path]))
    assert not target.exists()
    assert not tuple(tmp_path.glob('.snapshot.result.*.tmp'))


def _is_daily_source(group, record):
    return group == 'sources' and record.logical_path.startswith('knowledge/daily/')
