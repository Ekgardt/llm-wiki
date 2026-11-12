"""Parts of one immutable day must not reload its complete physical source."""


from tests.compile_archive_daily_sharing_helpers import _parts, _reader
from tests.compile_streamed_archive_batches_helpers import _materialized_daily


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
