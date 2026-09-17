"""A head of records that cannot be adopted must not hide the orphans behind it.

See docs/research/2026-09-17-bad-intents-do-not-use-up-the-adoption-bound.md.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_capture_intent_adoption import (
    _adopt,
    _adopted_ids,
    _coordinator,
    _damage,
    _publish_ready_intent,
    _queue,
    _skipped_ids,
)


def _bad_head_and_one_good(tmp_path: Path, queue, coordinator) -> tuple[list[str], str]:
    bad = [
        _publish_ready_intent(tmp_path, queue, coordinator, f"bad-{index}".encode())
        for index in range(2)
    ]
    for orphan in bad:
        _damage(tmp_path, orphan)
    good = _publish_ready_intent(tmp_path, queue, coordinator, b"good-behind-them")
    return [orphan["intent_id"] for orphan in bad], good["intent_id"]


def test_an_orphan_behind_a_full_window_of_bad_records_is_adopted(tmp_path):
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    bad, good = _bad_head_and_one_good(tmp_path, queue, coordinator)

    result = _adopt(queue, coordinator, tmp_path, limit=2)

    assert (_adopted_ids(result), sorted(_skipped_ids(result)), result["examined"]) == (
        [good],
        sorted(bad),
        3,
    )


def test_the_pass_stops_looking_after_its_bound_of_skips(tmp_path, monkeypatch):
    import capture_adoption

    monkeypatch.setattr(capture_adoption, "MAX_SKIPPED_INTENTS_PER_PASS", 2)
    queue, coordinator = _queue(tmp_path), _coordinator(tmp_path)
    bad, _good = _bad_head_and_one_good(tmp_path, queue, coordinator)

    result = _adopt(queue, coordinator, tmp_path, limit=1)

    assert (_adopted_ids(result), len(_skipped_ids(result))) == ([], 2)
