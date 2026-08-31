"""A checkpoint batch is named after the whole batch, not after its last event.

Measured on this vault 2026-08-30. With the drain window in place the state
lock stopped timing out and the drain reached the coordinator, where it failed
on every attempt with `occurrence_id is already bound to another event` — eight
times in five minutes, with 2 464 checkpoints queued behind it. Diffing the two
event bodies the coordinator compares showed a single differing field:

    stored    evidence_event_ids = ["b9bdf169...", "cc3d8ebd...", ...]
    requested evidence_event_ids = ["bf6e0938...", "25e9f587...", ...]

Same name, different membership. The name came from `items[-1]["event_id"]`, so
two batches ending at the same event were two operations wearing one name, and
a reservation refuses the second one permanently.

Every element of the evidence list is an event identifier that appears in the
queue once and is deleted on commit, so identical membership means one
operation retried — exactly the case a reservation exists to collapse.

See `docs/research/2026-08-30-a-batch-named-after-one-of-its-members.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402

_name = integration_adapter._batch_occurrence_id


def test_two_batches_ending_at_the_same_event_are_named_differently() -> None:
    """The failure that wedged the queue, stated as a property."""
    assert _name(["a", "b", "z"]) != _name(["c", "d", "z"])


def test_the_same_batch_retried_keeps_its_name() -> None:
    """Idempotency: a retry must be collapsed, not refused."""
    assert _name(["a", "b", "z"]) == _name(["a", "b", "z"])


def test_order_is_part_of_the_batch() -> None:
    """The journal is ordered, so a reordered batch is not the same batch."""
    assert _name(["a", "b"]) != _name(["b", "a"])


def test_a_longer_batch_is_not_the_same_as_its_prefix() -> None:
    """The wedge came from a batch and a differently sized one sharing a name."""
    assert _name(["a", "z"]) != _name(["a"])


def test_a_single_event_batch_still_has_a_name_of_its_own() -> None:
    assert _name(["only"]) != _name(["other"])


def test_the_name_fits_the_schema_field() -> None:
    """`occurrence_id` is a string of 1..256 characters."""
    name = _name([f"event-{index}" for index in range(100)])

    assert isinstance(name, str)
    assert 1 <= len(name) <= 256


def test_the_name_says_what_it_is() -> None:
    """A reader hitting a conflict should see at a glance what the id names."""
    assert _name(["a"]).startswith("batch:")


def test_the_name_is_not_one_of_the_members() -> None:
    """The regression guard: never fall back to naming a batch after an event."""
    evidence = ["first", "middle", "last"]

    assert _name(evidence) not in evidence
