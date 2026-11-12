"""The legacy maintenance row renews at its own pace after the guard learned per-lease pace.

Research: `docs/research/2026-09-14-a-heartbeat-beats-at-the-pace-of-its-own-lease.md`.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from tests.doctor_helpers import _build_root
from tests.slow_machine import LONG_TIMEOUT


def test_the_legacy_maintenance_row_is_renewed_before_it_runs_out(tmp_path):
    import generation_maintenance

    root, state_root, _ = _build_root(tmp_path)
    coordinator, lease = generation_maintenance.acquire_maintenance_owner(
        root, state_root, datetime.now(UTC)
    )

    guard = generation_maintenance.MaintenanceHeartbeat(coordinator, lease, deadline=time.monotonic() + LONG_TIMEOUT)
    generation_maintenance.release_maintenance_owner(coordinator, lease)

    assert (guard.interval, generation_maintenance.lease_seconds(lease)) == (
        generation_maintenance.MAINTENANCE_HEARTBEAT_SECONDS,
        generation_maintenance.MAINTENANCE_LEASE_SECONDS,
    )
