"""Parts of one immutable day must not reload its complete physical source."""
import hashlib
import io
import zipfile
from contextlib import contextmanager

import compile_controller as controller
import compile_memory as compiler

from tests.test_compile_streamed_archive_batches import _materialized_daily


def _parts(path):
    original = b'first entry\nsecond entry\n'
    digest = hashlib.sha256(original).hexdigest()
    return tuple(compiler.DailySnapshot(
        path, body, hashlib.sha256(body).hexdigest(), index, 2,
        start, start + len(body), original, digest,
    ) for index, (start, body) in enumerate(((0, original[:12]), (12, original[12:]))))


@contextmanager
def _reader(parts):
    inputs = compiler.CompileInputs(parts, (), ())
    output = io.BytesIO()
    digest = controller.compile_inputs_digest(inputs)
    controller._write_archive_snapshot(inputs, digest, output)
    with zipfile.ZipFile(output) as archive:
        yield controller.CompileInputArchive(archive, digest)


def test_daily_parts_share_original_content():
    parts = _parts('knowledge/daily/2026-10-01.md')
    with _reader(parts) as reader:
        restored = reader.planning_inputs().dailies
        assert restored == parts
        assert restored[0].original_content is restored[1].original_content


def test_daily_scope_reads_original_once_and_releases_at_next_day(monkeypatch):
    parts = _parts('knowledge/daily/2026-10-01.md') + _parts('knowledge/daily/2026-10-02.md')
    with _reader(parts) as reader:
        reads = []
        original = reader.archive.open

        def track(name, *args, **kwargs):
            reads.append(name)
            return original(name, *args, **kwargs)

        monkeypatch.setattr(reader.archive, 'open', track)
        records = reader.records('dailies')
        first = next(records)
        assert reads.count('blobs/' + first.original_sha256) == 1
        second = next(records)
        assert reads.count('blobs/' + first.original_sha256) == 1
        third = next(records)
        assert reads.count('blobs/' + first.original_sha256) == 2
        assert second.original_content is first.original_content
        assert third.original_content is not first.original_content
        assert tuple(_materialized_daily(part) for part in records) == parts[3:]
