"""A backlog must not make the cost of draining it grow with its own size.

Measured on this vault 2026-08-30. A journal outage between 08-28 14:47 and
08-29 21:27 stopped the project-checkpoint drain 2 728 times. It left 2 537
pending checkpoints — 3.7 MB inside a 6.7 MB `run/state.json`. The outage was
repaired on 08-29 and the queue still did not move: sampled every 20 seconds
with no session activity, the length stayed at 2 537.

The reason was that one drain cycle claimed, copied and replayed the entire
queue, then rewrote all of `run/state.json` three times — claim, persist,
commit — while asking for the state lock with `lock_timeout=0.5`. Cost scaled
with the backlog, the time allowed to pay it did not: 1 338 `Could not acquire
state lock` on 08-29 alone. The repair could not take effect because the
backlog itself prevented the drain.

A cycle now claims at most `PENDING_CLAIM_WINDOW` items from the head, so
recovery is linear in the backlog and never impossible. Nothing is dropped and
ordering is unchanged — the window is a prefix, and the committed batch is a
prefix of the window.

See `docs/research/2026-08-30-a-backlog-that-prevents-its-own-drain.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402


def _backlog(count: int) -> dict:
    queue = [{"event_id": f"e{index}"} for index in range(count)]
    return {"project_checkpoint_pending": {"demo": queue}}


def _claimed(state: dict, owner: str = "owner-1") -> list[dict]:
    claimed: list = []
    integration_adapter._claim_pending_state(state, "demo", owner, claimed)
    if not claimed:
        return []
    return claimed[0][0]


def test_a_cycle_claims_a_window_and_not_the_backlog() -> None:
    """2 537 pending events; one cycle takes a window, not all of them."""
    state = _backlog(2537)

    items = _claimed(state)

    assert len(items) == integration_adapter.PENDING_CLAIM_WINDOW


def test_the_window_is_the_head_of_the_queue() -> None:
    """Ordering is the contract: the claim is a prefix, so the commit is too."""
    state = _backlog(2537)

    items = _claimed(state)

    assert [item["event_id"] for item in items] == [
        f"e{index}" for index in range(integration_adapter.PENDING_CLAIM_WINDOW)
    ]


def test_a_short_queue_is_claimed_whole_exactly_as_before() -> None:
    state = _backlog(7)

    items = _claimed(state)

    assert len(items) == 7


def test_nothing_outside_the_window_is_claimed_or_lost() -> None:
    """The rest stays queued and unmarked, for the next cycle to take."""
    state = _backlog(2537)

    _claimed(state)
    queue = state["project_checkpoint_pending"]["demo"]

    assert len(queue) == 2537
    window = integration_adapter.PENDING_CLAIM_WINDOW
    assert all("claim_owner" in item for item in queue[:window])
    assert not any("claim_owner" in item for item in queue[window:])


def test_a_live_claim_by_another_owner_still_blocks_the_cycle() -> None:
    """Windowing must not weaken the claim; two drains must not overlap."""
    state = _backlog(2537)
    state["project_checkpoint_pending"]["demo"][0]["claim_owner"] = "someone-else"
    state["project_checkpoint_pending"]["demo"][0]["claim_until"] = 1 << 40

    assert _claimed(state) == []


def test_a_claim_held_beyond_the_window_does_not_block_the_cycle() -> None:
    """A stale claim on item 900 is no reason to refuse to drain item 0."""
    state = _backlog(2537)
    state["project_checkpoint_pending"]["demo"][900]["claim_owner"] = "someone-else"
    state["project_checkpoint_pending"]["demo"][900]["claim_until"] = 1 << 40

    assert len(_claimed(state)) == integration_adapter.PENDING_CLAIM_WINDOW


def test_the_window_covers_the_largest_batch_a_cycle_can_flush() -> None:
    """A smaller window would silently change how many events one batch carries."""
    assert integration_adapter.PENDING_CLAIM_WINDOW >= 100
    assert (
        integration_adapter.PENDING_CLAIM_WINDOW
        >= integration_adapter.MAX_PENDING_CHECKPOINT_ITEMS
    )
