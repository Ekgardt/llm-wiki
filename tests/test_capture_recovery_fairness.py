"""Missing older records cannot permanently hide a valid later capture."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import capture_adoption
import pytest

from tests.test_a_publication_that_stopped_half_way_is_finished import _publish_pending_intent
from tests.test_capture_intent_adoption import _coordinator, _publish_ready_intent, _queue


def _missing_prefix(queue, state, count):
    rows = [
        (f"{index:064x}", f"run/capture-intents/{state}/00/{index:064x}.json",
         "a" * 64, 2, state, "2000-01-01T00:00:00.000000+00:00")
        for index in range(count)
    ]
    with queue.connection() as database:
        database.executemany("INSERT INTO capture_intents VALUES (?,?,?,?,?,?)", rows)


@pytest.mark.parametrize("state", ["ready", "pending"])
def test_more_than_the_old_skip_cap_cannot_starve_valid_work(tmp_path, monkeypatch, state):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    publishers = {"ready": _publish_ready_intent, "pending": _publish_pending_intent}
    good = publishers[state](tmp_path, queue, coordinator, b"after-missing-prefix")
    _missing_prefix(queue, state, 257)  # One beyond the former 256-skip cutoff.
    monkeypatch.setattr(capture_adoption, "_stale_pending_cutoff", lambda _now: (
        datetime.now(timezone.utc) + timedelta(minutes=2)
    ).isoformat(timespec="microseconds"))
    sweeps = {
        "ready": (capture_adoption.adopt_orphaned_capture_intents, "adopted"),
        "pending": (capture_adoption.complete_pending_capture_intents, "completed"),
    }
    sweep, key = sweeps[state]
    result = sweep(queue, coordinator, state_root=tmp_path, limit=2)
    assert [entry["intent_id"] for entry in result[key]] == [good["intent_id"]]
    assert len(result["skipped"]) == 257
    assert result["examined"] == 258


def test_ready_reader_continues_after_an_equal_timestamp(tmp_path):
    queue = _queue(tmp_path)
    _missing_prefix(queue, "ready", 3)
    first = queue.ready_capture_intents_without_task(1)[0]
    rest = queue.ready_capture_intents_without_task(5, after=(first["updated_at"], first["intent_id"]))
    assert [row["intent_id"] for row in rest] == [f"{index:064x}" for index in (1, 2)]


def test_pending_reader_continues_after_an_equal_timestamp(tmp_path):
    queue = _queue(tmp_path)
    _missing_prefix(queue, "pending", 3)
    first = queue.pending_capture_intents(1)[0]
    rest = queue.pending_capture_intents(5, after=(first["updated_at"], first["intent_id"]))
    assert [row["intent_id"] for row in rest] == [f"{index:064x}" for index in (1, 2)]


@pytest.mark.parametrize("cursor", [(), ("stamp",), ("stamp", 1), ["stamp", "id"]])
def test_reader_refuses_malformed_cursor(tmp_path, cursor):
    queue = _queue(tmp_path)
    with pytest.raises(ValueError, match="cursor"):
        queue.ready_capture_intents_without_task(1, after=cursor)
