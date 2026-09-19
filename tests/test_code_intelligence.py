"""The normalized code-intelligence values the navigation path reads.

What the analyzer-run, claim and verified-batch records asserted went with them
on 2026-09-18: `docs/research/2026-09-18-the-superseded-plan-a-seam-leaves-the-code.md`.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError

import pytest
from code_intelligence import (
    PositionRange,
)
from corpus_snapshot import (
    SnapshotPolicy,
    SourceRecord,
    canonical_source_manifest_sha256,
)

SHA = tuple(character * 64 for character in "0123456789abcdef")
SQLITE_INT64_MAX = 2**63 - 1
SOURCE_CONTENT = {"source:a": b"name", "source:b": b"node"}
SOURCE_RECORDS = tuple(
    SourceRecord(
        source_id,
        f"{source_id[-1]}.py",
        hashlib.sha256(content).hexdigest(),
        len(content),
        "text/x-python",
        "python",
        None,
    )
    for source_id, content in SOURCE_CONTENT.items()
)
SNAPSHOT_POLICY = SnapshotPolicy((), (".",), False, None, 10, 100, 1000, 100, 100, 10)
SOURCE_MANIFEST_SHA256 = canonical_source_manifest_sha256(SOURCE_RECORDS, SNAPSHOT_POLICY)


def test_all_records_are_frozen_and_slotted() -> None:
    item = PositionRange(0, 1)
    assert "__dict__" not in dir(item)
    with pytest.raises(FrozenInstanceError):
        item.byte_start = 1  # type: ignore[misc]


@pytest.mark.parametrize("start,end", [(-1, 0), (0, -1), (True, 1), (0, False), (2, 1)])
def test_position_range_is_nonnegative_half_open(start: object, end: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        PositionRange(start, end)  # type: ignore[arg-type]


def test_position_range_nonempty_boundary() -> None:
    assert PositionRange(0, 0).byte_start == 0
    with pytest.raises(ValueError, match="claim.*non-empty"):
        PositionRange(0, 0).require_nonempty("claim range")
    assert PositionRange(0, 1).require_nonempty("claim range") == PositionRange(0, 1)


def test_position_range_accepts_signed_int64_max_and_rejects_overflow() -> None:
    assert PositionRange(SQLITE_INT64_MAX - 1, SQLITE_INT64_MAX).byte_end == SQLITE_INT64_MAX
    with pytest.raises(ValueError, match="byte_start.*signed int64"):
        PositionRange(SQLITE_INT64_MAX + 1, SQLITE_INT64_MAX + 1)
    with pytest.raises(ValueError, match="byte_end.*signed int64"):
        PositionRange(0, SQLITE_INT64_MAX + 1)


