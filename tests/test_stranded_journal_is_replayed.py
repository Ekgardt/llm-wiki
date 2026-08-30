"""A crashed writer's rollback journal fenced off the whole memory write path.

SQLite can only replay a rollback journal by opening the database read-write,
so every read-only open afterwards fails with "attempt to write a readonly
database". The callers of `open_readonly_operational_db` are validators, so the
adoption gate raised `reliability_v3_record_invalid` — a message naming neither
the file nor the cause — and every project checkpoint, every Markdown write and
the transaction health check refused with it.

Seen twice on the live vault on 2026-08-29, at 03:58 and again at 15:08, both
times with no process holding the journal. The second time 4 643 project
checkpoint events piled up undrained over a day and `run/state.json` grew to
10 MB before anyone looked. One read-write open cleared it and
`PRAGMA integrity_check` answered `ok` both times.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import reliable_memory  # noqa: E402


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "operational.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()
    _harden(path)
    return path


def _harden(path: Path) -> None:
    """Owner-only the way the product does it, not the way POSIX spells it.

    `chmod(0o600)` only toggles the read-only bit on Windows, where the check
    reads an ACL instead — so three tests here refused with "runtime file must
    be owner-only" on every Windows job on 2026-08-30. This is the same call
    the runtime makes when it creates such a file.
    """
    reliable_memory._harden_runtime_owner_only(path, 0o600)


def _strand_a_journal(path: Path) -> Path:
    """A journal with no live writer, the shape a crashed process leaves."""
    journal = Path(f"{path}-journal")
    journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 512)
    _harden(journal)
    return journal


def test_a_stranded_journal_is_replayed_and_the_open_succeeds(tmp_path: Path) -> None:
    path = _database(tmp_path)
    journal = _strand_a_journal(path)

    connection = reliable_memory.open_readonly_operational_db(
        path, tmp_path, max_bytes=1 << 20, owner_only=True
    )
    try:
        assert connection.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    finally:
        connection.close()
    assert not journal.exists()


def test_a_healthy_database_is_never_opened_read_write(tmp_path: Path) -> None:
    """No journal, no recovery: the ordinary path must not gain a write."""
    path = _database(tmp_path)
    calls: list[Path] = []

    original = reliable_memory._replayed_hot_journal
    reliable_memory._replayed_hot_journal = lambda target: (
        calls.append(target) or original(target)
    )
    try:
        connection = reliable_memory.open_readonly_operational_db(
            path, tmp_path, max_bytes=1 << 20, owner_only=True
        )
        connection.close()
    finally:
        reliable_memory._replayed_hot_journal = original

    assert calls == []


def test_a_refusal_that_is_not_about_a_journal_is_re_raised() -> None:
    error = sqlite3.OperationalError("no such table: t")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        reliable_memory._require_replayed_journal(Path("/nowhere/db.sqlite3"), error)


def test_a_refusal_with_no_journal_beside_it_is_re_raised(tmp_path: Path) -> None:
    """Read-only refusal without a journal is a permission problem, not this one."""
    path = _database(tmp_path)
    error = sqlite3.OperationalError("attempt to write a readonly database")

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        reliable_memory._require_replayed_journal(path, error)


def test_a_journal_that_will_not_clear_keeps_the_original_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    """A live writer holds a hot journal legitimately; refusing is right there."""
    path = _database(tmp_path)
    _strand_a_journal(path)
    monkeypatch.setattr(reliable_memory, "_replayed_hot_journal", lambda target: False)
    error = sqlite3.OperationalError("attempt to write a readonly database")

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        reliable_memory._require_replayed_journal(path, error)


def test_the_recovery_is_attempted_at_most_once(tmp_path: Path, monkeypatch) -> None:
    """One retry, never a loop: a second failure must reach the caller."""
    path = _database(tmp_path)
    _strand_a_journal(path)
    attempts: list[int] = []

    def never_clears(target: Path) -> bool:
        attempts.append(1)
        return True

    monkeypatch.setattr(reliable_memory, "_replayed_hot_journal", never_clears)

    with pytest.raises(sqlite3.OperationalError):
        reliable_memory.open_readonly_operational_db(
            path, tmp_path, max_bytes=1 << 20, owner_only=True
        )

    assert len(attempts) == 1
