"""The legacy maintenance row renews at its own pace after the guard learned per-lease pace.

Research: `docs/research/2026-09-14-a-heartbeat-beats-at-the-pace-of-its-own-lease.md`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from tests.slow_machine import SHORT_TIMEOUT
from tests.test_doctor import _build_root


def test_the_legacy_maintenance_row_is_renewed_before_it_runs_out(tmp_path):
    import doctor

    root, state_root, _ = _build_root(tmp_path)
    coordinator, lease = doctor._acquire_maintenance_owner(
        root, state_root, datetime.now(timezone.utc)
    )

    guard = doctor._MaintenanceHeartbeat(coordinator, lease, deadline=time.monotonic() + SHORT_TIMEOUT)
    doctor._release_maintenance_owner(coordinator, lease)

    assert (guard.interval, doctor._lease_seconds(lease)) == (
        doctor.MAINTENANCE_HEARTBEAT_SECONDS,
        doctor.MAINTENANCE_LEASE_SECONDS,
    )
