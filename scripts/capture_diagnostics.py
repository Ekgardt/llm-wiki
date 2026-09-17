"""Durable diagnostics for capture hooks — a lost capture must leave a trace.

Prompt and post-tool capture are best-effort by design: they must never break
the user's session, so every failure path returns quietly. Silence is not the
same as safety. A capture that fails without a record is indistinguishable
from a session that had nothing worth capturing, and the loss is invisible to
the user and to maintenance alike.

Every failure lands here:

* one bounded JSONL trail (`logs/capture-failures.jsonl`) carrying the reason,
* one counter per failure kind in `state.json` for the health surfaces.

Both are bounded: the trail is trimmed to the newest entries under a byte cap,
and the counter map keeps the most recent kinds only. Recording is itself
best-effort — diagnostics must never become the reason a hook fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    REPORTS_DIR,
    StateLockTimeout,
    atomic_write,
    load_state,
    update_state,
)
from secret_redact import redact_secrets  # noqa: E402

FAILURE_LOG = REPORTS_DIR / "capture-failures.jsonl"
MAX_FAILURE_LOG_BYTES = 256 * 1024
MAX_FAILURE_KINDS = 32
MAX_REASON_CHARS = 200
STATE_KEY = "capture_failures"

# How long a lost capture stays a live finding. A day plus the working week: a
# loss that has not recurred inside it has been dealt with or has stopped
# happening, and either way it is history rather than something to act on.
CAPTURE_RECENT_SECONDS = 7 * 24 * 3600
STATE_LOCK_TIMEOUT = 0.5


# A lost race is decided by the exception's type and code, never by its text
# (#26.3): another writer holds the resource now and the next session end or
# queue drain carries the same event, so it is retried work, not lost work.
CONTENTION_OWNERSHIP_CODES = frozenset({"owner_busy"})
# SQLITE_BUSY and SQLITE_LOCKED; the driver reports them from Python 3.11 on.
SQLITE_CONTENTION_CODES = frozenset({5, 6})


def _sqlite_contention(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in SQLITE_CONTENTION_CODES


def _typed_contention(error: BaseException) -> bool:
    from markdown_transaction import OperationBoundElsewhereError, ProjectPendingPriorError

    return isinstance(
        error, (ProjectPendingPriorError, OperationBoundElsewhereError, StateLockTimeout)
    )


# A queue owner that lost its intent fence lost authority, not data: the intent
# is durable before any worker runs, a lapsed worker fence leaves a lease to be
# recovered, and a capture fence lost before the task exists leaves a ready
# intent for adoption (docs/research/2026-09-11-a-lost-fence-is-not-a-lost-capture.md).
CONTENTION_QUEUE_CODES = frozenset({"intent_fence_lost"})


def _queue_contention(error: BaseException) -> bool:
    """Read the loaded queue module only: an error of its type cannot exist without it."""
    queue_module = sys.modules.get("memory_queue")
    error_type = getattr(queue_module, "QueueOperationError", None)
    if error_type is None or not isinstance(error, error_type):
        return False
    return error.code in CONTENTION_QUEUE_CODES


def is_contention(error: BaseException) -> bool:
    """Whether a failure is a writer race rather than a loss."""
    from operational_ownership import OperationalOwnershipError

    if isinstance(error, OperationalOwnershipError):
        return error.code in CONTENTION_OWNERSHIP_CODES
    return _typed_contention(error) or _sqlite_contention(error) or _queue_contention(error)


# A capture worker runs only over intents published durably before it starts, and
# a worker that fails leaves them for the next worker or for adoption: its failure
# is retried work, never a lost capture, whatever it raised. See
# `docs/research/2026-09-14-a-worker-that-failed-lost-no-capture.md`.
DURABLE_WORK_KINDS = frozenset({"adapter_capture_worker"})


class DurableWorkExhausted(RuntimeError):
    """The task behind a durable capture spent its last attempt and is now dead.

    Nothing retries a dead task: it waits for an operator's `redrive`. That is a
    loss to show, not work that is merely late. See
    `docs/research/2026-09-17-an-absent-provider-is-waited-for-and-a-spent-task-is-a-loss.md`.
    """


def _outcome_of(error: BaseException | None, kind: str = "") -> str:
    if isinstance(error, DurableWorkExhausted):
        return "lost"
    return "deferred" if _is_retried_work(error, kind) else "lost"


def _is_retried_work(error: BaseException | None, kind: str) -> bool:
    """Durable work a later worker takes up, or a writer race the next event repeats."""
    if kind in DURABLE_WORK_KINDS:
        return True
    return error is not None and is_contention(error)


def _safe_reason(reason: str) -> str:
    """One redacted line — reasons carry exception text, never payloads."""
    single_line = " ".join(str(reason).split())
    return redact_secrets(single_line)[:MAX_REASON_CHARS]


def _failure_record(
    kind: str,
    reason: str,
    slug: str | None,
    session_id: str | None,
    outcome: str,
) -> dict[str, str]:
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "kind": str(kind),
        "reason": _safe_reason(reason),
        "outcome": outcome,
    }
    if slug:
        record["slug"] = str(slug)
    if session_id:
        record["session"] = str(session_id)[:8]
    return record


def _trimmed_tail(lines: list[str], max_bytes: int) -> list[str]:
    """Newest lines that fit the byte cap, oldest dropped whole."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        used += len(line.encode("utf-8")) + 1
        if used > max_bytes:
            break
        kept.append(line)
    kept.reverse()
    return kept


def _existing_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


# Where an append is a seek and then a write (the Windows C runtime), two
# writers that seek to the same end overwrite each other, so there the write is
# made under a one-byte lock. The byte is far past the trail's cap and not byte
# 0: a Windows lock is mandatory, and a lock on real bytes would fail a reader
# while a line is being added. The wait is short and bounded because this runs
# inside hooks with budgets of a few seconds. Research:
# `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
TRAIL_LOCK_OFFSET = 1 << 30
TRAIL_LOCK_ATTEMPTS = 20
TRAIL_LOCK_PAUSE_SECONDS = 0.025


def _append_locking():
    """The byte-range locking module where append is not atomic, else None."""
    if os.name != "nt":
        return None
    import msvcrt

    return msvcrt


def _lock_trail(descriptor: int, locking, pause) -> bool:
    """Take the trail's lock byte, or say within a bounded wait that it is held."""
    for _ in range(TRAIL_LOCK_ATTEMPTS):
        os.lseek(descriptor, TRAIL_LOCK_OFFSET, os.SEEK_SET)
        try:
            locking.locking(descriptor, locking.LK_NBLCK, 1)
        except OSError:
            pause(TRAIL_LOCK_PAUSE_SECONDS)
            continue
        return True
    return False


def _unlock_trail(descriptor: int, locking) -> None:
    # Closing the descriptor releases the byte too, so a failed unlock is not a
    # failed write and must not turn one line into two.
    with suppress(OSError):
        os.lseek(descriptor, TRAIL_LOCK_OFFSET, os.SEEK_SET)
        locking.locking(descriptor, locking.LK_UNLCK, 1)


def _write_trail_line(descriptor: int, data: bytes, locking, pause=time.sleep) -> bool:
    """One append-mode write, serialized where the system does not do it itself.

    The descriptor is in append mode, so the write lands at the end wherever the
    lock left the file pointer. False means the lock was never free: nothing was
    written, and nothing of anyone else's was overwritten.
    """
    if locking is None:
        os.write(descriptor, data)
        return True
    if not _lock_trail(descriptor, locking, pause):
        return False
    try:
        os.write(descriptor, data)
    finally:
        _unlock_trail(descriptor, locking)
    return True


# A trim reads every line and replaces the whole file, so it must not cross an
# append: the replaced inode carries the appended line away with it. The lock
# lives on a sidecar file, as `run/state.json`'s does, because a lock taken on
# the trail itself is a lock on a file the trim is about to rename away.
# Research: `docs/research/2026-09-17-a-trim-never-drops-a-line-it-did-not-read.md`.
TRAIL_LOCK_SUFFIX = ".lock"


def _trail_lock_path() -> Path:
    """Read from `FAILURE_LOG` at the call: the trail's path is a module setting."""
    return FAILURE_LOG.with_name(FAILURE_LOG.name + TRAIL_LOCK_SUFFIX)


def _hold_trail_lock(descriptor: int) -> None:
    """One non-blocking exclusive OS lock attempt on the sidecar's first byte."""
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _await_trail_lock(descriptor: int, pause) -> bool:
    """True once the sidecar lock is ours; False when the bounded wait ran out."""
    for _ in range(TRAIL_LOCK_ATTEMPTS):
        try:
            _hold_trail_lock(descriptor)
        except OSError:
            pause(TRAIL_LOCK_PAUSE_SECONDS)
            continue
        return True
    return False


def _open_trail_lock() -> int | None:
    path = _trail_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        return os.open(path, flags, 0o600)
    except OSError:
        return None


@contextmanager
def trail_lock(pause=time.sleep):
    """Exclusive access to the trail file; yields whether the lock was taken.

    Closing the descriptor releases the lock on both systems, so nothing is left
    held by a process that died holding it.
    """
    descriptor = _open_trail_lock()
    if descriptor is None:
        yield False
        return
    try:
        yield _await_trail_lock(descriptor, pause)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _write_failure_line(line: str) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(FAILURE_LOG, flags, 0o600)
    except OSError:
        return False
    try:
        return _write_trail_line(descriptor, line.encode("utf-8"), _append_locking())
    except OSError:
        return False
    finally:
        os.close(descriptor)


def _append_failure_line(record: dict[str, str]) -> bool:
    """Add one line with a single append-mode write; True when it was written.

    Reading the trail, adding a line and writing the whole file back lost a line
    whenever two hooks failed together, which is when hooks fail. See
    `docs/research/2026-09-17-two-failures-at-once-both-reach-the-trail.md`.

    The trail's own lock is taken around the open and the write, so the file this
    line is appended to is not one a trim has already replaced. A line is never
    lost to that lock: when the bounded wait runs out the line is written anyway.
    """
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with trail_lock():
        return _write_failure_line(line)


def _trim_under_lock() -> None:
    if FAILURE_LOG.stat().st_size <= MAX_FAILURE_LOG_BYTES:
        return
    kept = _trimmed_tail(_existing_lines(FAILURE_LOG), MAX_FAILURE_LOG_BYTES * 3 // 4)
    atomic_write(FAILURE_LOG, "\n".join(kept) + "\n")


def _trim_failure_log() -> None:
    """Cut an overgrown trail to three quarters of its cap, in one step.

    Only ever under the trail's lock: reading every line and writing the file back
    would otherwise erase an append that landed in between. A trim that cannot take
    the lock leaves the trail alone and the next recorded failure trims instead —
    the cut is below the cap, so it is owed once per quarter-cap of failures.
    """
    try:
        with trail_lock() as held:
            if held:
                _trim_under_lock()
    except OSError:
        return


def _bump_counter(state: dict, record: dict[str, str]) -> None:
    counters = state.setdefault(STATE_KEY, {})
    entry = counters.get(record["kind"])
    if not isinstance(entry, dict):
        entry = {}
    deferred = int(entry.get("deferred", 0)) + int(record["outcome"] == "deferred")
    counters[record["kind"]] = {
        "count": int(entry.get("count", 0)) + 1,
        "deferred": deferred,
        "last_at": record["at"],
        "last_reason": record["reason"],
    }
    _drop_oldest_kinds(counters)


def _drop_oldest_kinds(counters: dict) -> None:
    """Keep the most recently seen kinds so the counter map stays bounded."""
    if len(counters) <= MAX_FAILURE_KINDS:
        return
    ranked = sorted(counters.items(), key=lambda kv: str(kv[1].get("last_at", "")))
    for kind, _ in ranked[: len(counters) - MAX_FAILURE_KINDS]:
        counters.pop(kind, None)


def record_capture_failure(
    kind: str,
    reason: str,
    *,
    error: BaseException | None = None,
    slug: str | None = None,
    session_id: str | None = None,
) -> None:
    """Record one failed capture. Never raises — diagnostics never break a hook.

    With the exception in hand the record says whether the write was lost or
    deferred by a writer race; without it, a failure is a loss.
    """
    record = _failure_record(kind, reason, slug, session_id, _outcome_of(error, kind))
    written: list[bool] = []

    def _under_the_state_lock(state: dict) -> None:
        written.append(_trail_written(record, trim=True))
        _bump_counter(state, record)

    try:
        update_state(_under_the_state_lock, lock_timeout=STATE_LOCK_TIMEOUT)
    except Exception:  # noqa: BLE001 - the trail below still records the loss
        pass
    if not any(written):
        # No lock, no trim: an append alone cannot erase anyone else's line.
        _trail_written(record, trim=False)


def _trail_written(record: dict[str, str], *, trim: bool) -> bool:
    """Write the trail, whatever it raises: the counter still records the loss."""
    try:
        written = _append_failure_line(record)
        if trim:
            _trim_failure_log()
    except Exception:  # noqa: BLE001 - diagnostics never break a hook
        return False
    return written


def _counter_entries(state: dict) -> dict[str, dict]:
    counters = state.get(STATE_KEY)
    if not isinstance(counters, dict):
        return {}
    return {kind: entry for kind, entry in counters.items() if isinstance(entry, dict)}


def _lost_count(entry: dict) -> int:
    return max(int(entry.get("count", 0)) - int(entry.get("deferred", 0)), 0)


def capture_failure_totals(state: dict) -> dict[str, int]:
    """Lost captures per kind: every record minus the ones a writer race deferred."""
    totals = {kind: _lost_count(entry) for kind, entry in _counter_entries(state).items()}
    return {kind: count for kind, count in totals.items() if count}


def capture_deferred_totals(state: dict) -> dict[str, int]:
    """Captures a writer race deferred, per kind: retried, not lost."""
    totals = {
        kind: int(entry.get("deferred", 0))
        for kind, entry in _counter_entries(state).items()
    }
    return {kind: count for kind, count in totals.items() if count}


def _trail_pointer() -> str:
    """Send the reader to the trail only when the trail is actually there.

    The counters live in state.json and the trail is a separate best-effort
    file. It can be absent — an unwritable reports directory, a state root that
    moved, ordinary cleanup — and pointing at a file that is not there wastes
    the one moment the operator is paying attention.
    """
    if FAILURE_LOG.is_file():
        return "see `logs/capture-failures.jsonl`."
    return "the trail at `logs/capture-failures.jsonl` is missing; reasons are in `run/state.json`."


def _recorded_moment(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("last_at", ""))


def last_capture_failure_at(state: dict) -> str:
    """The most recent moment any kind was recorded, or an empty string."""
    counters = state.get(STATE_KEY)
    if not isinstance(counters, dict):
        return ""
    moments = [_recorded_moment(entry) for entry in counters.values()]
    return max(moments, default="")


def _moment_age_seconds(moment: str, now: datetime) -> float | None:
    try:
        recorded = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return (now - recorded).total_seconds()


def capture_failure_is_live(state: dict, now: datetime | None = None) -> bool:
    """Whether a lost capture is a live finding rather than history.

    The counter is evidence of a real loss and nothing zeroes it on its own, so
    a finding tied to the count alone could never return to green — and a report
    that is always red stops being read, which is the opposite of why the
    counter exists. The finding therefore covers the last seven days: a new loss
    makes it true again at once, and a quiet week returns it to green while the
    totals stay visible.

    State written before the moment was recorded has no timestamp to judge, and
    counts as history.
    """
    if not sum(capture_failure_totals(state).values()):
        return False
    age = _moment_age_seconds(last_capture_failure_at(state), now or datetime.now())
    if age is None:
        return False
    return age <= CAPTURE_RECENT_SECONDS


def capture_failure_line(state: dict) -> str:
    """One SessionStart line naming lost captures, empty when nothing was lost.

    The count is cumulative and nothing clears it on its own, so the line has to
    say when this last happened. Without that, a loss fixed months ago reads
    exactly like one from this morning.
    """
    if not capture_failure_is_live(state):
        return ""
    totals = capture_failure_totals(state)
    lost = sum(totals.values())
    detail = ", ".join(f"{kind} {count}" for kind, count in sorted(totals.items()))
    last_at = last_capture_failure_at(state)
    when = f", last at {last_at}" if last_at else ""
    return (
        f"- **Capture**: ⚠️ {lost} capture(s) lost ({detail}{when}) — "
        f"{_trail_pointer()} Retire with "
        f"`uv run python scripts/capture_diagnostics.py --clear`."
    )


def clear_capture_failures() -> dict[str, int]:
    """Retire the counters and report what was retired.

    Deliberate, never automatic: the count records a loss that really happened,
    and only a person can say it has been dealt with.
    """
    retired: dict[str, int] = {}

    def mutate(state: dict) -> None:
        retired.update(capture_failure_totals(state))
        state.pop(STATE_KEY, None)

    update_state(mutate, lock_timeout=STATE_LOCK_TIMEOUT)
    return retired


def _print_summary(state: dict) -> int:
    totals = capture_failure_totals(state)
    for kind, count in sorted(capture_deferred_totals(state).items()):
        print(f"{kind}: {count} deferred by a writer race (retried, not lost)")
    if not totals:
        print("capture_diagnostics: no capture failures recorded")
        return 0
    for kind, count in sorted(totals.items()):
        print(f"{kind}: {count}")
    print(f"trail: {FAILURE_LOG}")
    return 1


def _print_cleared(retired: dict[str, int]) -> int:
    if not retired:
        print("capture_diagnostics: nothing to clear")
        return 0
    for kind, count in sorted(retired.items()):
        print(f"cleared {kind}: {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture failure diagnostics.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when any capture failure has been recorded.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Retire the recorded counters after dealing with them.",
    )
    args = parser.parse_args()
    if args.clear:
        return _print_cleared(clear_capture_failures())
    status = _print_summary(load_state())
    return status if args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
