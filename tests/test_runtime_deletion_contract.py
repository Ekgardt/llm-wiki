from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _transaction_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE "transaction" (
                id TEXT PRIMARY KEY, state TEXT, updated_at TEXT,
                artifacts_pruned_at TEXT
            );
            CREATE TABLE project_leases (
                project TEXT PRIMARY KEY, expires_at TEXT
            );
            CREATE TABLE writer_owners (
                gate_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            CREATE TABLE maintenance_owners (
                owner_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            """
        )
        connection.executemany(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?)',
            [
                ("tx-active", "applying", now.isoformat(), None),
                ("tx-conflict", "conflicted", now.isoformat(), None),
                ("tx-quarantine", "quarantined", now.isoformat(), None),
                ("tx-undo", "committed", now.isoformat(), None),
            ],
        )
        future = (now + timedelta(minutes=5)).isoformat()
        connection.execute("INSERT INTO project_leases VALUES ('p', ?)", (future,))
        connection.execute("INSERT INTO writer_owners VALUES ('global', 1, ?)", (future,))
        connection.execute("INSERT INTO maintenance_owners VALUES ('doctor', 1, ?)", (future,))
    (state_root / "run/transactions/tx-undo").mkdir(parents=True)
    return database


def _queue_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, state TEXT, result_reference TEXT
            );
            CREATE TABLE queue_ownership (
                role TEXT PRIMARY KEY, token TEXT, pid INTEGER, expires_at TEXT
            );
            """
        )
        connection.execute("INSERT INTO tasks VALUES ('task', 'dead', NULL)")
        connection.execute(
            "INSERT INTO queue_ownership VALUES ('worker', 'token', 1, ?)",
            ((now + timedelta(minutes=5)).isoformat(),),
        )
    results = state_root / "run/queue-results"
    results.mkdir()
    (results / "orphan.result").write_text("retained", encoding="utf-8")
    return database


def test_run_deletion_reports_every_contract_blocker(tmp_path, monkeypatch):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _transaction_db(state_root, now)
    _queue_db(state_root, now)
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    result = doctor._run_deletion_check(state_root, now)

    assert {item["code"] for item in result["blockers"]} == {
        "transaction_nonterminal",
        "transaction_conflicted",
        "transaction_quarantined",
        "transaction_undo_retained",
        "queue_task_retained",
        "queue_result_retained",
        "project_lease_live",
        "writer_live",
        "queue_worker_live",
        "maintenance_owner_live",
    }
    assert result["allowed"] is False
    assert "tx-active" not in json.dumps(result)


def test_run_deletion_is_allowed_only_when_no_blocker_exists(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)

    assert doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc)
    ) == {"allowed": True, "blockers": []}


def test_installers_and_doctor_never_remove_run_source_contract():
    root = Path(__file__).resolve().parent.parent
    sources = [
        (root / "install.sh").read_text(encoding="utf-8"),
        (root / "install.ps1").read_text(encoding="utf-8"),
        (root / "scripts/doctor.py").read_text(encoding="utf-8"),
    ]
    forbidden = ("rm -rf $STATE_ROOT/run", "Remove-Item $STATE_ROOT\\run", "rmtree(state_root / \"run\")")
    assert all(token not in source for source in sources for token in forbidden)


def test_deletion_blocks_every_legacy_and_retained_queue_artifact(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    run = state_root / "run"
    legacy = run / "queue"
    results = run / "queue-results"
    quarantine = run / "queue-quarantine"
    for directory in (legacy, results, quarantine):
        directory.mkdir(parents=True, exist_ok=True)
    (legacy / "pending.json").write_text("{}", encoding="utf-8")
    (legacy / "leased.processing").write_text("broken", encoding="utf-8")
    (results / "retained.tmp").write_text("result", encoding="utf-8")
    (quarantine / "bad.json").write_text("{}", encoding="utf-8")

    result = doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc), deadline=float("inf")
    )

    assert {item["code"] for item in result["blockers"]} >= {
        "legacy_queue_retained",
        "legacy_queue_malformed",
        "queue_result_retained",
        "queue_quarantine_retained",
    }


def test_deletion_blocks_source_state_and_any_partial_database_error(
    tmp_path, monkeypatch
):
    import doctor

    state_root = tmp_path / "state"
    path = state_root / "run/queue.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks(id TEXT PRIMARY KEY,state TEXT);
            INSERT INTO tasks VALUES('task','ready');
            CREATE TABLE source_fences(daily_id TEXT);
            INSERT INTO source_fences VALUES('2026-01-01');
            CREATE TABLE source_failures(logical_path TEXT);
            INSERT INTO source_failures VALUES('knowledge/daily/2026-01-01.md');
            """
        )
    real = doctor._readonly_database

    class BrokenAfterRows:
        def __enter__(self):
            self.database = real(path, state_root, max_bytes=doctor.MAX_OPERATIONAL_DB_BYTES)
            return self

        def __exit__(self, *args):
            self.database.close()

        def execute(self, sql, parameters=()):
            if "source_failures" in sql:
                raise sqlite3.DatabaseError("corrupt tail")
            return self.database.execute(sql, parameters)

    monkeypatch.setattr(doctor, "_readonly_database", lambda *args, **kwargs: BrokenAfterRows())

    queue = doctor._queue_v2_check(
        state_root, datetime.now(timezone.utc), float("inf")
    )
    deletion = doctor._run_deletion_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"queue": queue},
    )

    assert queue["details"]["read_error"] is True
    assert "queue_state_unreadable" in {
        item["code"] for item in deletion["blockers"]
    }


def test_deletion_reuses_collected_checks_without_rescanning(tmp_path, monkeypatch):
    import doctor

    transaction = {
        "id": "transactions",
        "status": "ok",
        "details": {"deletion_codes": []},
    }
    queue = {"id": "queue", "status": "ok", "details": {"deletion_codes": []}}
    archive = {"id": "archives", "status": "ok", "details": {"deletion_codes": []}}
    monkeypatch.setattr(
        doctor,
        "_transaction_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )
    monkeypatch.setattr(
        doctor,
        "_queue_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )

    result = doctor._run_deletion_check(
        tmp_path,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"transactions": transaction, "queue": queue, "archives": archive},
    )

    assert result == {"allowed": True, "blockers": []}
