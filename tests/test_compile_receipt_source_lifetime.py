"""Receipt discovery must not retain bodies of previously visited daily files."""
import gc
import math
import weakref
from dataclasses import replace

import compile_memory as cm
import pytest
from markdown_transaction import MarkdownCoordinator
from reliable_memory import sha256_bytes


@pytest.fixture
def selection(tmp_path):
    (tmp_path / "knowledge/daily").mkdir(parents=True)
    return cm._receipt_predicate(MarkdownCoordinator(tmp_path, tmp_path))


def _daily(selection, name, body):
    logical = f"knowledge/daily/{name}.md"
    (selection.vault / logical).write_bytes(body)
    return logical, sha256_bytes(body)


def test_receipt_scan_releases_previous_daily_before_reading_next(selection, monkeypatch):
    first = _daily(selection, "2026-10-18", b"first source\n")
    second = _daily(selection, "2026-10-19", b"second source\n")
    references = []
    parse = cm._iter_daily_parts
    read = cm._open_daily_source

    def observe_parts(*args, **kwargs):
        for part in parse(*args, **kwargs):
            references.append(weakref.ref(part))
            yield part

    def observe_read(path, **kwargs):
        gc.collect()
        assert all(reference() is None for reference in references)
        return read(path, **kwargs)

    monkeypatch.setattr(cm, "_iter_daily_parts", observe_parts)
    monkeypatch.setattr(cm, "_open_daily_source", observe_read)
    assert not selection(*first)
    assert not selection(*second)
    assert not selection(*first)


def _part_snapshots(selection, logical):
    with selection._source_parts(logical) as parts:
        return tuple(replace(part, original_content=part.original_content[:]) for part in parts)


def test_receipt_scan_rechecks_current_source_and_changed_bytes(selection, monkeypatch):
    source = _daily(selection, "2026-10-18", b"original\n")
    reads = []
    read = cm._open_daily_source

    def observe(path, **kwargs):
        reads.append(path)
        return read(path, **kwargs)

    monkeypatch.setattr(cm, "_open_daily_source", observe)
    original = _part_snapshots(selection, source[0])
    assert _part_snapshots(selection, source[0]) == original
    updated = _daily(selection, "2026-10-18", b"changed source\n")
    assert _part_snapshots(selection, updated[0])[0].sha256 == updated[1]
    assert len(reads) == 3
    assert original[0].sha256 == source[1]


def test_receipt_scan_preserves_error_and_can_retry_read(selection, monkeypatch):
    first = _daily(selection, "2026-10-18", b"first source\n")
    second = _daily(selection, "2026-10-19", b"second source\n")
    assert not selection(*first)
    read = cm._open_daily_source

    def fail(_path, **kwargs):
        raise OSError("unreadable source")

    monkeypatch.setattr(cm, "_open_daily_source", fail)
    with pytest.raises(OSError, match="unreadable source"):
        selection(*second)
    monkeypatch.setattr(cm, "_open_daily_source", read)
    assert not selection(*second)
    assert _part_snapshots(selection, second[0])[0].sha256 == second[1]


def test_receipt_scan_expired_deadline_does_not_read(selection, monkeypatch):
    source = _daily(selection, "2026-10-18", b"source\n")
    selection.deadline = -math.inf

    def fail(_path, **kwargs):
        pytest.fail("expired discovery must not read")

    monkeypatch.setattr(cm, "_open_daily_source", fail)
    with pytest.raises(TimeoutError, match="discovery deadline"):
        selection(*source)
