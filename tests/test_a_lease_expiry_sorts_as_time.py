"""A lease written on a whole second expires on that second, not up to a second later.

Research: docs/research/2026-09-17-a-lock-names-the-process-not-only-its-number.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import operational_ownership as ownership
import pytest


@dataclass
class _Clock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _registry(state_root: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock):
    markdown_transaction.initialize_coordinator_v3_candidate(
        state_root / "run" / "markdown-transactions-v3.candidate.sqlite3", source_v2=None
    )
    current = ownership.ProcessIdentity(pid=31001, start_identity="test-process:1")
    monkeypatch.setattr(ownership, "current_process_identity", lambda: current)
    return ownership.OwnershipRegistry(state_root, clock=clock)


def test_a_lease_taken_on_a_whole_second_is_lost_half_a_second_after_it_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    registry = _registry(tmp_path, monkeypatch, clock)
    lease = registry.acquire("doctor", scope="global", actor_id="actor-a", token="a" * 32)
    clock.value = lease.expires_at + timedelta(milliseconds=500)

    with pytest.raises(ownership.OperationalOwnershipError) as refusal:
        registry.heartbeat(lease)

    assert refusal.value.code == "owner_fence_lost"


def test_both_timestamp_shapes_are_read_and_one_is_written() -> None:
    whole = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    written = ownership._timestamp(whole)
    read = (
        ownership._parse_timestamp("2026-09-17T12:00:00Z"),
        ownership._parse_timestamp(written),
    )
    assert (written, read) == ("2026-09-17T12:00:00.000000Z", (whole, whole))
