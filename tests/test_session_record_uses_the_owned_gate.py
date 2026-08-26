"""A queued capture keeps its session record instead of losing it silently.

`write_session_evidence` claimed the canonical Markdown writer lease itself, so
inside the capture worker — which already holds an owner lease — every write
raised `owner_identity_conflict`. The writer never raises by contract, so the
record vanished without a trace: this vault held no session record between the
2026-08-24 backfill and 2026-08-26 while captures kept reporting success.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest  # noqa: E402
import session_evidence  # noqa: E402

from tests.test_queue_v3_capture_links import _coordinator, _queue  # noqa: E402


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "raw" / "sessions").mkdir(parents=True)
    return vault


@pytest.fixture()
def _owned(tmp_path: Path):
    import operational_ownership

    queue = _queue(tmp_path)
    coordinator = _coordinator(tmp_path)
    registry = operational_ownership.OwnershipRegistry(tmp_path)
    owner = registry.acquire("queue-worker", scope="worker:capture-recovery")
    try:
        with queue.queue_owner(
            role="queue-worker", scope="worker:capture-recovery", parent=owner
        ):
            yield coordinator, owner
    finally:
        registry.release(owner)


def test_the_record_is_written_while_the_worker_holds_the_writer_gate(
    tmp_path: Path, _owned
) -> None:
    coordinator, owner = _owned
    fields = {
        "session": "owned-gate",
        "host": "claude",
        "event": "session_end",
        "captured_at": "2026-08-26T21:00:00+00:00",
    }
    written = session_evidence.write_session_evidence(
        coordinator.vault,
        fields,
        "**user:** keep this\n\n**assistant:** kept\n",
        coordinator=coordinator,
        owner=owner,
    )
    assert written is not None
    assert written.read_text(encoding="utf-8").count("keep this") == 1
