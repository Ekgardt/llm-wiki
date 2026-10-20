"""Receipt discovery must not retain bodies of previously visited daily files."""
import gc
import math
import weakref

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
    parse = cm._daily_parts
    read = cm._read_daily_source

    def observe_parts(*args, **kwargs):
        parts = parse(*args, **kwargs)
        references.extend(weakref.ref(part) for part in parts)
        return parts

    def observe_read(path):
        gc.collect()
        assert all(reference() is None for reference in references)
        return read(path)

    monkeypatch.setattr(cm, "_daily_parts", observe_parts)
    monkeypatch.setattr(cm, "_read_daily_source", observe_read)
    assert not selection(*first)
    assert not selection(*second)
    assert not selection(*first)


def test_receipt_scan_reuses_current_source_but_rereads_changed_bytes(selection, monkeypatch):
    source = _daily(selection, "2026-10-18", b"original\n")
    reads = []
    read = cm._read_daily_source

    def observe(path):
        reads.append(path)
        return read(path)

    monkeypatch.setattr(cm, "_read_daily_source", observe)
    original = selection._source_parts(source[0])
    assert selection._source_parts(source[0]) is original
    updated = _daily(selection, "2026-10-18", b"changed source\n")
    assert selection._source_parts(updated[0])[0].sha256 == updated[1]
    assert len(reads) == 2
    assert original[0].sha256 == source[1]


def test_receipt_scan_preserves_error_and_can_retry_read(selection, monkeypatch):
    first = _daily(selection, "2026-10-18", b"first source\n")
    second = _daily(selection, "2026-10-19", b"second source\n")
    assert not selection(*first)
    read = cm._read_daily_source

    def fail(_path):
        raise OSError("unreadable source")

    monkeypatch.setattr(cm, "_read_daily_source", fail)
    with pytest.raises(OSError, match="unreadable source"):
        selection(*second)
    monkeypatch.setattr(cm, "_read_daily_source", read)
    assert not selection(*second)
    assert selection._source_parts(second[0])[0].sha256 == second[1]


def test_receipt_scan_expired_deadline_does_not_read(selection, monkeypatch):
    source = _daily(selection, "2026-10-18", b"source\n")
    selection.deadline = -math.inf

    def fail(_path):
        pytest.fail("expired discovery must not read")

    monkeypatch.setattr(cm, "_read_daily_source", fail)
    with pytest.raises(TimeoutError, match="discovery deadline"):
        selection(*source)
