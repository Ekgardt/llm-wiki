"""Lint returns only after fresh full-byte verification of its owned sources."""
import os

import evidence_resolver
import lint_memory
import pytest
from reliable_memory import sha256_bytes


def _source_and_reference(tmp_path):
    source = b"## [evt-1] event\nverified quote\n"
    daily = tmp_path / "knowledge/daily/2026-01-01.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source)
    start = source.index(b"verified quote")
    reference = (f"daily:2026-01-01 sha256:{sha256_bytes(source)} "
                 f"block:evt-1 bytes:{start}-{start + len(b'verified quote')}")
    return daily, source, reference


def test_lint_reads_a_shared_daily_once_then_rechecks_it(tmp_path, monkeypatch):
    daily, source, reference = _source_and_reference(tmp_path)
    note = tmp_path / "knowledge/notes/page.md"
    note.parent.mkdir(parents=True)
    note.write_text((f"- `{reference}`\n") * 4, encoding="utf-8")
    reads = []
    original = evidence_resolver._flat_source

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(evidence_resolver, "_flat_source", read)
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    assert lint_memory.check_evidence_references([note]) == []
    assert reads == [daily, daily]
    assert daily.read_bytes() == source


def test_an_owned_source_changed_with_restored_timestamps_is_refused(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    info = daily.stat()
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    with pytest.raises(evidence_resolver.EvidenceResolutionError, match="changed"):
        with resolver.captured_sources() as captured:
            assert captured.resolve(reference).bytes == b"verified quote"
            daily.write_bytes(source.replace(b"verified", b"modified"))
            os.utime(daily, ns=(info.st_atime_ns, info.st_mtime_ns))


def test_an_owned_source_removed_after_resolution_is_refused(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    with pytest.raises(evidence_resolver.EvidenceResolutionError, match="disappeared"):
        with resolver.captured_sources() as captured:
            captured.resolve(reference)
            daily.unlink()


def test_independent_operations_capture_new_bytes(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    with resolver.captured_sources() as captured:
        assert captured.resolve(reference).bytes == b"verified quote"
    daily.write_bytes(source.replace(b"verified", b"modified"))
    with resolver.captured_sources() as captured:
        with pytest.raises(evidence_resolver.EvidenceResolutionError, match="hash mismatch"):
            captured.resolve(reference)


def test_captured_resolution_preserves_the_complete_evidence_result(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    expected = resolver.resolve(reference)
    with resolver.captured_sources() as captured:
        assert captured.resolve(reference) == expected


def test_claim_lint_reuses_source_bytes_without_shortening_quotes(tmp_path, monkeypatch):
    from tests.test_claim_v2_versioned_consumers import _page, _record

    record, source = _record("e\u0301 " + "x" * 17_000)
    daily = tmp_path / "knowledge/daily/2026-10-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source)
    notes = tmp_path / "knowledge/notes"
    notes.mkdir()
    pages = [notes / f"page-{number}.md" for number in range(4)]
    for page in pages:
        page.write_bytes(_page([record]))
    reads = []
    original = evidence_resolver._flat_source

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(evidence_resolver, "_flat_source", read)
    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    assert lint_memory.check_claim_schemas(pages) == []
    assert reads == [daily, daily]


def test_claim_lint_rechecks_sources_before_returning_findings(tmp_path, monkeypatch):
    from tests.test_claim_v2_versioned_consumers import _page, _record

    record, source = _record("verified")
    daily = tmp_path / "knowledge/daily/2026-10-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(source)
    page = tmp_path / "knowledge/notes/page.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(_page([record]))
    original = lint_memory._require_evidence_matches

    def change_after_check(resolver, claim):
        original(resolver, claim)
        daily.write_bytes(source.replace(b"verified", b"modified"))

    monkeypatch.setattr(lint_memory, "ROOT", tmp_path)
    monkeypatch.setattr(lint_memory, "_require_evidence_matches", change_after_check)
    with pytest.raises(evidence_resolver.EvidenceResolutionError, match="changed"):
        lint_memory.check_claim_schemas([page])


def test_a_closed_capture_cannot_return_old_live_source_bytes(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    with resolver.captured_sources() as captured:
        captured.resolve(reference)
    daily.write_bytes(source.replace(b"verified", b"modified"))
    with pytest.raises(RuntimeError, match="closed"):
        captured.resolve(reference)
    assert captured.sources == {}
    assert captured.byte_resolver._immutable_sources == {}
    assert resolver._immutable_sources == {}


def test_a_failed_body_releases_its_owned_source_bytes(tmp_path):
    daily, source, reference = _source_and_reference(tmp_path)
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    with pytest.raises(ValueError, match="body refused"):
        with resolver.captured_sources() as captured:
            captured.resolve(reference)
            raise ValueError("body refused")
    assert captured.closed
    assert captured.sources == {}
    assert captured.byte_resolver._immutable_sources == {}


def test_a_captured_historical_part_matches_the_independent_reader(tmp_path):
    from tests.test_evidence_resolver import _long_day, _reference

    day = _long_day(150)
    start, end = evidence_resolver._daily_part_bounds(day)[1]
    part = day[start:end]
    marker = part.index(b"quote-")
    quote = part[marker:part.index(b"\n", marker)]
    block = part[part.index(b"## [") + 4:part.index(b"]")].decode()
    daily = tmp_path / "knowledge/daily/2026-01-02.md"
    daily.parent.mkdir(parents=True)
    daily.write_bytes(day + _long_day(20))
    reference = _reference("2026-01-02", part, block, marker, marker + len(quote))
    resolver = evidence_resolver.EvidenceResolver(tmp_path)
    expected = resolver.resolve(reference)
    with resolver.captured_sources() as captured:
        assert captured.resolve(reference) == expected
        assert captured.resolve(reference) == expected


def test_captured_archive_reads_keep_the_independent_verification(tmp_path, monkeypatch):
    from archive_daily import DailyArchiver

    from tests.test_evidence_resolver import _authorize_archive_source, _reference, _write_bag

    source = b"# day\n## [evt-1] event\narchived bytes\n"
    state = _authorize_archive_source(tmp_path, source, monkeypatch)
    bag = _write_bag(tmp_path, "2026-01-01", source)
    DailyArchiver._seal(bag)
    start = source.index(b"archived bytes")
    reference = _reference("2026-01-01", source, "evt-1", start, len(source) - 1)
    resolver = evidence_resolver.EvidenceResolver(tmp_path, state_root=state)
    expected = resolver.resolve(reference)
    with resolver.captured_sources() as captured:
        assert captured.resolve(reference) == expected
        second = _write_bag(tmp_path, "2026-01-01", source, suffix="two")
        DailyArchiver._seal(second)
        with pytest.raises(evidence_resolver.EvidenceResolutionError, match="ambiguous"):
            captured.resolve(reference)
