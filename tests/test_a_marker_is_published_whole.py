"""A maintenance marker is published whole, and a torn one left by a crash is taken back.

Audit 2026-09-26 B-21, docs/research/2026-09-26-a-marker-is-published-whole.md.
"""
from __future__ import annotations

import os

import operational_ownership as ownership

from tests.test_a_dead_owner_is_reclaimed_without_its_marker import MARKER, _candidate


def test_a_torn_marker_with_no_owner_does_not_stop_the_next_pass(tmp_path) -> None:
    _candidate(tmp_path)
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / MARKER).write_bytes(b"")

    lease, marker = ownership.acquire_scheduled_owner("nightly", state_root=tmp_path)

    assert (lease.role, marker.relative_path) == ("nightly", MARKER)


def test_an_unreadable_torn_file_is_not_a_proof(tmp_path) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / MARKER).write_bytes(b"")

    assert ownership._marker_is_torn(tmp_path / "run" / "absent.lock", tmp_path) is False


def test_the_published_marker_leaves_no_staging_file(tmp_path) -> None:
    ownership._publish_marker(tmp_path, MARKER, b"123\n")

    assert sorted(os.listdir(tmp_path / "run")) == ["maintenance.lock"]
