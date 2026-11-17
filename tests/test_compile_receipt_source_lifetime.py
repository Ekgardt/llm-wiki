"""Receipt discovery must not retain bodies of previously visited daily files."""
import gc
import math
import weakref

import compile_receipts
import pytest

from tests.compile_paths import point_compile_path
from tests.compile_receipt_source_lifetime_helpers import (
    _daily,
    _part_snapshots,
    selection,  # noqa: F401
)


def test_receipt_scan_releases_previous_daily_before_reading_next(selection, monkeypatch):  # noqa: F811
    first = _daily(selection, "2026-10-18", b"first source\n")
    second = _daily(selection, "2026-10-19", b"second source\n")
    references = []
    parse = compile_receipts.iter_daily_parts
    read = compile_receipts.open_daily_source

    def observe_parts(*args, **kwargs):
        for part in parse(*args, **kwargs):
            references.append(weakref.ref(part))
            yield part

    def observe_read(path, **kwargs):
        gc.collect()
        assert all(reference() is None for reference in references)
        return read(path, **kwargs)

    point_compile_path(monkeypatch, "iter_daily_parts", observe_parts)
    point_compile_path(monkeypatch, "open_daily_source", observe_read)
    assert not selection(*first)
    assert not selection(*second)
    assert not selection(*first)


def test_receipt_scan_rechecks_current_source_and_changed_bytes(selection, monkeypatch):  # noqa: F811
    source = _daily(selection, "2026-10-18", b"original\n")
    reads = []
    read = compile_receipts.open_daily_source

    def observe(path, **kwargs):
        reads.append(path)
        return read(path, **kwargs)

    point_compile_path(monkeypatch, "open_daily_source", observe)
    original = _part_snapshots(selection, source[0])
    assert _part_snapshots(selection, source[0]) == original
    updated = _daily(selection, "2026-10-18", b"changed source\n")
    assert _part_snapshots(selection, updated[0])[0].sha256 == updated[1]
    assert len(reads) == 3
    assert original[0].sha256 == source[1]


def test_receipt_scan_preserves_error_and_can_retry_read(selection, monkeypatch):  # noqa: F811
    first = _daily(selection, "2026-10-18", b"first source\n")
    second = _daily(selection, "2026-10-19", b"second source\n")
    assert not selection(*first)
    read = compile_receipts.open_daily_source

    def fail(_path, **kwargs):
        raise OSError("unreadable source")

    point_compile_path(monkeypatch, "open_daily_source", fail)
    with pytest.raises(OSError, match="unreadable source"):
        selection(*second)
    point_compile_path(monkeypatch, "open_daily_source", read)
    assert not selection(*second)
    assert _part_snapshots(selection, second[0])[0].sha256 == second[1]


def test_receipt_scan_expired_deadline_does_not_read(selection, monkeypatch):  # noqa: F811
    source = _daily(selection, "2026-10-18", b"source\n")
    selection.deadline = -math.inf

    def fail(_path, **kwargs):
        pytest.fail("expired discovery must not read")

    point_compile_path(monkeypatch, "open_daily_source", fail)
    with pytest.raises(TimeoutError, match="discovery deadline"):
        selection(*source)
