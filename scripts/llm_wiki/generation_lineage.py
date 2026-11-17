"""Active pointers, activation history and the generations retention keeps."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path


def pointer_is(database: sqlite3.Connection, expected_active: str | None) -> bool:
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    return row is not None and row["active_generation_id"] == expected_active


def pointer_matches(database: sqlite3.Connection, active: str | None) -> bool:
    """Like `_pointer_is`, but a missing pointer row is corruption, not a mismatch."""
    current = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    if current is None:
        raise ValueError("catalog active pointer is missing")
    return current["active_generation_id"] == active


def record_activation(
    database: sqlite3.Connection, selected_id: str | None, timestamp: str
) -> None:
    """Clearing the pointer records nothing; pointing it somewhere records that."""
    if selected_id is None:
        return
    database.execute(
        "INSERT INTO activation_history(generation_id, activated_at) VALUES (?, ?)",
        (selected_id, timestamp),
    )


def generation_in_use(database: sqlite3.Connection, identifier: str) -> bool:
    """Active now, or ever activated: either way the registration must be kept."""
    if pointer_matches(database, identifier):
        return True
    historical = database.execute(
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier,),
    ).fetchone()
    return historical is not None


def _active_generation(database: sqlite3.Connection) -> str | None:
    """The generation the pointer names; a missing pointer row is corruption."""
    row = database.execute(
        "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise ValueError("catalog active pointer is missing")
    return row["active_generation_id"]


def _parent_generation(database: sqlite3.Connection, identifier: str) -> str | None:
    row = database.execute(
        "SELECT parent_generation_id FROM generations WHERE generation_id = ?",
        (identifier,),
    ).fetchone()
    if row is None:
        return None
    return row["parent_generation_id"]


def retained_generation_ids(database: sqlite3.Connection, ancestors: int) -> tuple[str, ...]:
    """The live set: the active generation plus the spares retention keeps.

    The spares are the first ones `_fallback_order` would try: the generations
    activated before this one, newest first, then the active's ancestors along
    `parent_generation_id`. Walking the parent chain alone kept no spare after a
    full rebuild, which records no parent, and the prune then removed the one
    generation the fallback would have used (audit C-27,
    docs/research/2026-09-25-retention-keeps-the-fallbacks-first-choice.md). A
    cycle or a dangling parent ends the walk rather than extending it.
    """
    active = _active_generation(database)
    if active is None:
        return ()
    retained = [active]
    for identifier in _spare_candidates(database, active, ancestors):
        if len(retained) > ancestors:
            break
        _append_unseen_identifier(retained, identifier)
    return tuple(retained)


def _append_unseen_identifier(retained: list[str], identifier: str) -> None:
    if identifier not in retained:
        retained.append(identifier)


def _spare_candidates(database: sqlite3.Connection, active: str, ancestors: int) -> list[str]:
    """Earlier activations newest first, then the parent chain, each bounded by `ancestors`."""
    rows = database.execute(
        "SELECT generation_id FROM activation_history WHERE generation_id != ? "
        "GROUP BY generation_id ORDER BY MAX(sequence) DESC LIMIT ?",
        (active, max(ancestors, 0)),
    ).fetchall()
    return [str(row[0]) for row in rows] + _parent_chain(database, active, ancestors)


def _parent_chain(database: sqlite3.Connection, active: str, ancestors: int) -> list[str]:
    chain: list[str] = []
    identifier: str | None = active
    for _ in range(ancestors):
        identifier = _parent_generation(database, identifier)
        if identifier is None or identifier in chain:
            break
        chain.append(identifier)
    return chain


def require_active_root(retained: tuple[str, ...]) -> None:
    """No active pointer is no root, and without a root nothing is provably
    unreachable. `_repair_active_pointer` clears the pointer when no candidate
    validates; collecting then would destroy the very material a repair reads.
    That state is for a person to look at, not for a collector to act on."""
    if not retained:
        raise ValueError("catalog has no active generation")


def require_activated(database: sqlite3.Connection, identifier: str) -> None:
    """Only a superseded generation is prunable.

    A registration that has never been activated is either an abandoned
    publication or one happening right now, and nothing readable here tells the
    two apart: `register` returns before `activate` is called, so a build in
    flight looks exactly like an abort. `discard_unactivated` is the path for
    that case, and it runs under the publication fence.
    """
    activated = database.execute(
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier,),
    ).fetchone()
    if activated is None:
        raise ValueError("generation was never activated")


# What no writer has touched for this long is not in flight: every builder finishes,
# registration to activation, within a 15-minute budget. See
# `docs/research/2026-09-14-an-abandoned-publication-is-collected.md`.
ABANDONED_AFTER_SECONDS = 24 * 60 * 60
_MAX_ENTRIES_DATED = 256


def untouched_for(path: Path, seconds: float) -> bool:
    """Whether nothing in this directory (itself or an immediate entry) changed for `seconds`.

    An unreadable directory counts as touched just now: doubt keeps it.
    """
    return time.time() - _newest_write(path) >= seconds


def _newest_write(path: Path) -> float:
    try:
        stamps = [path.lstat().st_mtime]
        stamps.extend(_dated_entry_writes(path))
    except OSError:
        return time.time()
    return max(stamps)


def _dated_entry_writes(path: Path) -> list[float]:
    with os.scandir(path) as entries:
        return [
            entry.stat(follow_symlinks=False).st_mtime
            for _index, entry in zip(range(_MAX_ENTRIES_DATED), entries)
        ]


def require_retained_ancestors(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("retained_ancestors must be a non-negative integer")
    if value < 0:
        raise ValueError("retained_ancestors must be a non-negative integer")
