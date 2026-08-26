"""What counts as ownership that blocks the Reliability V3 coordinator migration.

The rule used to be "any row in `project_leases`, `writer_owners` or
`maintenance_owners`". None of those tables is carried into v3 either way — the
v2 row collector never reads them — so the rule decided only whether the
migration was allowed to start.

That made adoption unreachable on any vault that had ever taken a project lease.
A lease row is deleted only by the holder that releases it, so an agent that
exits without releasing leaves the row behind for good. Measured on the owner's
vault on 2026-08-26: 56 project leases, 55 of them expired on 2026-08-21, plus
one released `doctor` maintenance owner. Adoption refused every time, so
`session_end` failed with `legacy_protocol_unquiesced` and not one session had
ever been captured on that machine.

The rule is now liveness, which is what the queue half of the same adoption
already used: only a task still in `leased` state refuses there. Doubt still
fails closed — an expiry that is absent or unreadable counts as live.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import markdown_transaction
import pytest
from markdown_transaction import MarkdownCoordinator
from reliable_memory import OperationalDatabaseContractError


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge/notes").mkdir(parents=True)
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _stamp(offset: timedelta) -> str:
    moment = datetime.now(timezone.utc) + offset
    return moment.isoformat().replace("+00:00", "Z")


def _ownership_sql(table: str, expires_at: str) -> str:
    statements = {
        "project_leases": (
            "INSERT INTO project_leases VALUES "
            f"('demo', 'token', 1, 'actor', '{expires_at}', '{expires_at}')"
        ),
        "writer_owners": (
            "INSERT INTO writer_owners VALUES "
            f"('global', 'token', 1, 2, '{expires_at}', '{expires_at}', "
            f"'{expires_at}', 1)"
        ),
    }
    return statements[table]


def _seed(coordinator: MarkdownCoordinator, statement: str) -> None:
    with sqlite3.connect(coordinator.database_path) as database:
        database.execute(statement)
        database.commit()


def _migrate(coordinator: MarkdownCoordinator, candidate: Path):
    return markdown_transaction.initialize_coordinator_v3_candidate(
        candidate, source_v2=coordinator.database_path
    )


@pytest.mark.parametrize("table", ["project_leases", "writer_owners"])
def test_an_expired_owner_does_not_block_the_migration(
    vault: Path, state_root: Path, table: str
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    _seed(coordinator, _ownership_sql(table, _stamp(-timedelta(hours=1))))
    candidate = state_root / "run" / f"candidate-expired-{table}.sqlite3"

    _migrate(coordinator, candidate)

    with sqlite3.connect(candidate) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] != 0
        assert database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


@pytest.mark.parametrize("table", ["project_leases", "writer_owners"])
def test_a_live_owner_still_blocks_the_migration(
    vault: Path, state_root: Path, table: str
) -> None:
    coordinator = MarkdownCoordinator(vault, state_root)
    _seed(coordinator, _ownership_sql(table, _stamp(timedelta(hours=1))))
    candidate = state_root / "run" / f"candidate-live-{table}.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        _migrate(coordinator, candidate)

    assert getattr(raised.value, "code", None) == "coordinator_v2_ambiguous_ownership"


def test_an_unreadable_expiry_counts_as_live(vault: Path, state_root: Path) -> None:
    """Fail closed: a stamp nobody can read is not proof the owner is gone."""
    coordinator = MarkdownCoordinator(vault, state_root)
    _seed(coordinator, _ownership_sql("project_leases", "not-a-timestamp"))
    candidate = state_root / "run" / "candidate-unreadable.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        _migrate(coordinator, candidate)

    assert getattr(raised.value, "code", None) == "coordinator_v2_ambiguous_ownership"


def _widen_maintenance_owners(coordinator: MarkdownCoordinator) -> None:
    """Mirror the live shape: the expiry columns arrived by a later ALTER."""
    with sqlite3.connect(coordinator.database_path) as database:
        columns = markdown_transaction._coordinator_table_columns(
            database, "maintenance_owners"
        )
        added = {"heartbeat_at": "TEXT", "expires_at": "TEXT", "fencing_epoch": "INTEGER"}
        for name, kind in added.items():
            _add_column(database, columns, name, kind)
        database.commit()


def _add_column(
    database: sqlite3.Connection, columns: tuple[str, ...], name: str, kind: str
) -> None:
    if name in columns:
        return
    database.execute(f"ALTER TABLE maintenance_owners ADD COLUMN {name} {kind}")


def test_a_released_maintenance_owner_does_not_block_the_migration(
    vault: Path, state_root: Path
) -> None:
    """Release writes the year-one sentinel rather than deleting the row."""
    coordinator = MarkdownCoordinator(vault, state_root)
    _widen_maintenance_owners(coordinator)
    sentinel = "0001-01-01T00:00:00+00:00"
    _seed(
        coordinator,
        "INSERT INTO maintenance_owners "
        "(owner_name, owner_token, process_id, acquired_at, heartbeat_at, "
        "expires_at, fencing_epoch) VALUES "
        f"('doctor', '', 0, '{_stamp(-timedelta(hours=1))}', '{sentinel}', "
        f"'{sentinel}', 70)",
    )
    candidate = state_root / "run" / "candidate-released-maintenance.sqlite3"

    _migrate(coordinator, candidate)

    with sqlite3.connect(candidate) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] != 0


def test_an_owner_row_without_an_expiry_column_still_blocks(
    vault: Path, state_root: Path
) -> None:
    """A v2 database old enough to lack the column offers nothing to date a row by."""
    coordinator = MarkdownCoordinator(vault, state_root)
    _seed(
        coordinator,
        "INSERT INTO maintenance_owners "
        "(owner_name, owner_token, process_id, acquired_at) "
        f"VALUES ('doctor', 'token', 1, '{_stamp(-timedelta(hours=1))}')",
    )
    candidate = state_root / "run" / "candidate-no-expiry-column.sqlite3"

    with pytest.raises(OperationalDatabaseContractError) as raised:
        _migrate(coordinator, candidate)

    assert getattr(raised.value, "code", None) == "coordinator_v2_ambiguous_ownership"


def test_a_naive_expiry_is_read_as_utc(vault: Path, state_root: Path) -> None:
    """Some v2 rows carry no offset; every writer of these tables meant UTC."""
    naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)

    live = markdown_transaction._expiry_is_live(
        naive.isoformat(), datetime.now(timezone.utc)
    )

    assert live is False
