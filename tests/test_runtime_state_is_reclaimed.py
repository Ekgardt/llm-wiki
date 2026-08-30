"""Recovery that a session hook cannot perform must still happen, unattended.

Measured on this vault 2026-08-30, two findings from the same cause:

A project-checkpoint backlog of 2 485 grew `run/state.json` to 6.7 MB. Hooks
held the state lock almost continuously, and the drain — which asks for it with
a 0.5 s budget, correctly, because a person is waiting — lost it every time.
Eight consecutive forced drains, each refused in 0.6 s, queue unmoved. The
backlog could not clear itself from the path that created it.

39 `.state.json.*.tmp` files, 272 MB, all complete JSON, the oldest from 08-26.
`atomic_write` stages, fsyncs, then renames; a process killed in between leaves
the staged file and nothing collects it.

See `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402
import reclaim_runtime_state as reclaim  # noqa: E402


def _staged(directory: Path, name: str, age_seconds: float, size: int = 16) -> Path:
    path = directory / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    import os

    os.utime(path, (stamp, stamp))
    return path


def test_an_abandoned_temporary_is_reclaimed(tmp_path: Path) -> None:
    """The 272 MB case: complete, fsynced, never published, never collected."""
    orphan = _staged(tmp_path, ".state.json.deadbeef.tmp", 7200, size=1024)

    result = reclaim.sweep_orphan_temporaries(tmp_path)

    assert not orphan.exists()
    assert result == {"removed": 1, "bytes": 1024}


def test_a_temporary_a_live_writer_could_own_is_left_alone(tmp_path: Path) -> None:
    """A write takes well under a second; an hour old cannot be in flight."""
    fresh = _staged(tmp_path, ".state.json.feedface.tmp", 5)

    result = reclaim.sweep_orphan_temporaries(tmp_path)

    assert fresh.exists()
    assert result == {"removed": 0, "bytes": 0}


def test_the_sweep_touches_nothing_but_staged_files(tmp_path: Path) -> None:
    """`run/` holds operational state under a deletion contract. Do not guess."""
    state = tmp_path / "state.json"
    state.write_text("{}")
    lock = tmp_path / "state.json.lock"
    lock.write_text("1234")
    old_state = _staged(tmp_path, "queue.sqlite3", 7200)

    reclaim.sweep_orphan_temporaries(tmp_path)

    assert state.exists()
    assert lock.exists()
    assert old_state.exists()


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert reclaim.sweep_orphan_temporaries(tmp_path / "absent") == {
        "removed": 0,
        "bytes": 0,
    }


def test_the_unattended_drain_waits_where_the_hook_may_not(monkeypatch) -> None:
    """The whole point: recovery is allowed the patience a hook is denied."""
    budgets: list[float] = []

    def record(slug, queue_key, **kwargs):
        budgets.append(kwargs["state_lock_seconds"])

    monkeypatch.setattr(integration_adapter, "_pending_backlog_slugs", lambda: ["demo"])
    monkeypatch.setattr(integration_adapter, "_pending_backlog_depth", lambda slug: 0)
    monkeypatch.setattr(integration_adapter, "_drain_project_checkpoints", record)

    integration_adapter.drain_pending_backlog(5.0)

    assert budgets == [integration_adapter.BACKLOG_STATE_LOCK_SECONDS]
    assert integration_adapter.BACKLOG_STATE_LOCK_SECONDS > (
        integration_adapter.PENDING_STATE_LOCK_SECONDS
    )


def test_the_drain_reports_what_it_actually_moved(monkeypatch) -> None:
    depths = iter([2485, 2385])

    monkeypatch.setattr(integration_adapter, "_pending_backlog_slugs", lambda: ["demo"])
    monkeypatch.setattr(integration_adapter, "_pending_backlog_depth", lambda slug: next(depths))
    monkeypatch.setattr(
        integration_adapter, "_drain_project_checkpoints", lambda *a, **k: None
    )

    result = integration_adapter.drain_pending_backlog(5.0)

    assert result["drained"] == {"demo": 100}


def test_a_stuck_project_cannot_hang_the_pass(monkeypatch) -> None:
    """Unattended work needs a bound, or one bad project stops the night."""
    visited: list[str] = []

    def slow(slug, queue_key, **kwargs):
        visited.append(slug)
        time.sleep(0.05)

    monkeypatch.setattr(
        integration_adapter, "_pending_backlog_slugs", lambda: ["a", "b", "c", "d"]
    )
    monkeypatch.setattr(integration_adapter, "_pending_backlog_depth", lambda slug: 0)
    monkeypatch.setattr(integration_adapter, "_drain_project_checkpoints", slow)

    integration_adapter.drain_pending_backlog(0.0)

    assert visited == ["a"]


def test_the_hook_path_keeps_its_own_impatience() -> None:
    """A session hook must not block; that budget is not what changed here."""
    assert integration_adapter.PENDING_STATE_LOCK_SECONDS == 0.5
