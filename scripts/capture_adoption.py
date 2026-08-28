#!/usr/bin/env python3
"""Adopt capture intents that were published durably but never dispatched.

The capture path publishes a durable intent — the whole redacted record, on
disk under `run/capture-intents/ready/`, plus a row in `capture_intents` — and
only then enqueues the task that gives it a worker. Between those two steps the
publisher holds one capture owner and one intent fence. If either is lost there,
the intent is committed and the task never exists, and nothing looks for it:
`recover_expired_leases` recovers a task whose *lease* expired, which
presupposes a task.

This module is the missing relay. It is the transactional-outbox sweeper for
that gap: read the committed-but-undispatched rows, and dispatch them.

Three properties of a capture intent are what make adopting one safe, and all
three are load-bearing:

* it is create-only — the file is published with `create_only=True` and the row
  moves `pending -> ready` once, so re-reading it cannot race a rewrite;
* it is self-verifying — the row carries `intent_sha256`, so a record whose
  bytes moved becomes a named skip instead of a task that fails at the worker;
* it is self-addressing — `relative_path`, `intent_id` and `intent_sha256` are
  exactly the payload the adapter enqueues, so adoption rebuilds a byte-identical
  payload from the record alone, without the session or the original process.

What adoption never does is reuse or weaken the lost fence. The publisher's
fence expired because the publisher is gone; that is the fence working. An
adopting pass takes its own capture owner and its own intent fence under its own
pid, so every clause `_require_live_capture_fence` checks is satisfied honestly.

Idempotence is not reinvented here either. `enqueue_capture_task_replay_safe`
looks for an existing link on `intent_id` before enqueueing and returns the
existing binding if it finds one, and the task it writes carries the dedupe key
`capture:{intent_id}:{handler_version}` in the same commit as the link row.
Adopting the same intent twice therefore yields one task.

See `docs/research/2026-08-28-adopting-an-orphaned-intent.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from integration_adapter import (  # noqa: E402
    CAPTURE_HANDLER_VERSION,
    MAX_CAPTURE_INTENT_BYTES,
    ROOT,
    STATE_ROOT,
)

# One pass adopts at most this many intents. The bound is required rather than
# tidy: the sweeper runs at the head of the capture worker, which is spawned on
# every session end and is expected to finish, so an unbounded pass over a
# backlog would delay the capture that just woke it. The live vault publishes on
# the order of ten intents a day, so 32 clears about three days — a weekend of
# failures goes in the first Monday capture. A bound defers work and never drops
# it: the sweeper runs again on the next capture and again every night.
MAX_ADOPTED_INTENTS_PER_PASS = 32


def _verified_intent_bytes(state_root: Path, record: dict[str, Any]) -> bytes:
    """Read the record's own bytes and prove they are the ones it names."""
    from reliable_memory import read_runtime_bytes, sha256_bytes

    payload = read_runtime_bytes(
        state_root / str(record["relative_path"]),
        state_root,
        max_bytes=MAX_CAPTURE_INTENT_BYTES,
        owner_only=True,
    )
    if sha256_bytes(payload) != str(record["intent_sha256"]):
        raise ValueError("intent_digest_changed")
    if len(payload) != int(record["byte_size"]):
        raise ValueError("intent_size_changed")
    return payload


@contextmanager
def _fresh_capture_authority(queue: object, coordinator: object, intent_id: str):
    """Take this pass's own owner and fence — never the publisher's lost one."""
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    try:
        fence = coordinator.acquire_intent_fence(
            intent_id, mode="capture", owner=owner
        )
        try:
            yield owner, fence
        finally:
            coordinator.release_intent_fence(fence)
    finally:
        registry.release(owner)


def _adoption_payload(record: dict[str, Any]) -> dict[str, str]:
    """The adapter's payload, rebuilt from the durable record's own columns."""
    return {
        "intent_id": str(record["intent_id"]),
        "intent_path": str(record["relative_path"]),
        "intent_sha256": str(record["intent_sha256"]),
    }


def _adopt_one(
    queue: object, coordinator: object, state_root: Path, record: dict[str, Any]
) -> str:
    """Give one orphaned intent a task. Returns the task id."""
    intent_id = str(record["intent_id"])
    _verified_intent_bytes(state_root, record)
    payload = _adoption_payload(record)
    with _fresh_capture_authority(queue, coordinator, intent_id) as (owner, fence):
        binding = queue.enqueue_capture_task_replay_safe(
            "flush",
            CAPTURE_HANDLER_VERSION,
            payload,
            intent_id=intent_id,
            intent_path=payload["intent_path"],
            intent_sha256=payload["intent_sha256"],
            capture_fence=fence,
            owner=owner,
        )
    return str(binding.task_id)


def _skip(record: dict[str, Any], error: BaseException) -> dict[str, str]:
    """A refusal names the intent and the reason; the record is left untouched."""
    return {
        "intent_id": str(record["intent_id"]),
        "reason": f"{type(error).__name__}: {error}",
    }


def _adopt_records(
    queue: object,
    coordinator: object,
    state_root: Path,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    adopted: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for record in records:
        try:
            task_id = _adopt_one(queue, coordinator, state_root, record)
        except Exception as error:  # noqa: BLE001 - one bad record must not stop the pass
            skipped.append(_skip(record, error))
            continue
        adopted.append({"intent_id": str(record["intent_id"]), "task_id": task_id})
    return {
        "examined": len(records),
        "adopted": adopted,
        "skipped": skipped,
    }


def adopt_orphaned_capture_intents(
    queue: object,
    coordinator: object,
    *,
    state_root: Path,
    limit: int = MAX_ADOPTED_INTENTS_PER_PASS,
) -> dict[str, Any]:
    """Dispatch ready intents that no task was ever created for.

    Never raises for a single bad record: a record whose bytes moved, or whose
    intent is still fenced by a process that has not let go, is reported as a
    skip so the rest of the pass still runs.
    """
    reader = getattr(queue, "ready_capture_intents_without_task", None)
    if reader is None:
        return {"examined": 0, "adopted": [], "skipped": [], "reason": "unsupported"}
    return _adopt_records(queue, coordinator, Path(state_root), reader(limit))


def adopt_in_active_vault(*, limit: int = MAX_ADOPTED_INTENTS_PER_PASS) -> dict[str, Any]:
    """Run one adoption pass against the installed vault's active runtime."""
    from markdown_transaction import active_markdown_coordinator
    from memory_queue import active_memory_queue

    vault = Path(ROOT).resolve(strict=True)
    state_root = Path(STATE_ROOT).resolve(strict=True)
    return adopt_orphaned_capture_intents(
        active_memory_queue(vault, state_root),
        active_markdown_coordinator(vault, state_root),
        state_root=state_root,
        limit=limit,
    )


def _report(result: dict[str, Any]) -> None:
    print(
        f"capture adoption: examined {result['examined']}, "
        f"adopted {len(result['adopted'])}, skipped {len(result['skipped'])}"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=MAX_ADOPTED_INTENTS_PER_PASS
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the orphaned intents without giving them tasks",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run(args.limit)
    _report(adopt_in_active_vault(limit=args.limit))
    return 0


def _dry_run(limit: int) -> int:
    from memory_queue import active_memory_queue

    queue = active_memory_queue(
        Path(ROOT).resolve(strict=True), Path(STATE_ROOT).resolve(strict=True)
    )
    records = queue.ready_capture_intents_without_task(limit)
    print(f"orphaned capture intents: {len(records)}")
    for record in records:
        print(f"  {record['intent_id']} {record['updated_at']} {record['byte_size']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
