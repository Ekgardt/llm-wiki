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
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import REPORTS_DIR, load_state, update_state  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

FAILURE_LOG = REPORTS_DIR / "capture-failures.jsonl"
MAX_FAILURE_LOG_BYTES = 256 * 1024
MAX_FAILURE_KINDS = 32
MAX_REASON_CHARS = 200
STATE_KEY = "capture_failures"
STATE_LOCK_TIMEOUT = 0.5


def _safe_reason(reason: str) -> str:
    """One redacted line — reasons carry exception text, never payloads."""
    single_line = " ".join(str(reason).split())
    return redact_secrets(single_line)[:MAX_REASON_CHARS]


def _failure_record(
    kind: str,
    reason: str,
    slug: str | None,
    session_id: str | None,
) -> dict[str, str]:
    record = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "kind": str(kind),
        "reason": _safe_reason(reason),
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


def _append_failure_line(record: dict[str, str]) -> None:
    """Append to the trail and trim it back under the cap, best effort."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        lines = _existing_lines(FAILURE_LOG) + [line]
        kept = _trimmed_tail(lines, MAX_FAILURE_LOG_BYTES)
        FAILURE_LOG.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


def _bump_counter(state: dict, record: dict[str, str]) -> None:
    counters = state.setdefault(STATE_KEY, {})
    entry = counters.get(record["kind"])
    previous = int(entry.get("count", 0)) if isinstance(entry, dict) else 0
    counters[record["kind"]] = {
        "count": previous + 1,
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
    slug: str | None = None,
    session_id: str | None = None,
) -> None:
    """Record one lost capture. Never raises — diagnostics never break a hook."""
    record = _failure_record(kind, reason, slug, session_id)
    try:
        _append_failure_line(record)
    except Exception:  # noqa: BLE001 - the counter still records the loss
        pass
    try:
        update_state(
            lambda state: _bump_counter(state, record),
            lock_timeout=STATE_LOCK_TIMEOUT,
        )
    except Exception:  # noqa: BLE001
        pass


def capture_failure_totals(state: dict) -> dict[str, int]:
    """Failure count per kind as recorded in state.json."""
    counters = state.get(STATE_KEY)
    if not isinstance(counters, dict):
        return {}
    return {
        kind: int(entry.get("count", 0))
        for kind, entry in counters.items()
        if isinstance(entry, dict)
    }


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


def _last_failure_at(state: dict) -> str:
    """The most recent moment any kind was recorded, or an empty string."""
    counters = state.get(STATE_KEY)
    if not isinstance(counters, dict):
        return ""
    moments = [_recorded_moment(entry) for entry in counters.values()]
    return max(moments, default="")


def capture_failure_line(state: dict) -> str:
    """One SessionStart line naming lost captures, empty when nothing was lost.

    The count is cumulative and nothing clears it on its own, so the line has to
    say when this last happened. Without that, a loss fixed months ago reads
    exactly like one from this morning.
    """
    totals = capture_failure_totals(state)
    lost = sum(totals.values())
    if not lost:
        return ""
    detail = ", ".join(f"{kind} {count}" for kind, count in sorted(totals.items()))
    last_at = _last_failure_at(state)
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
