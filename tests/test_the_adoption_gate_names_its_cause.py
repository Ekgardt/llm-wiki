"""The adoption gate says what it saw, and the queue waits out a busy database.

See `docs/research/2026-09-23-the-adoption-gate-names-its-cause-and-waits-out-contention.md`.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.slow_machine import LONG_TIMEOUT

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import installed_memory_repair  # noqa: E402
import markdown_transaction  # noqa: E402
import memory_queue  # noqa: E402

from tests.test_doctor import _adopt, _build_root  # noqa: E402


def test_an_invalid_record_names_the_cause_behind_its_code(tmp_path: Path) -> None:
    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    (state_root / "run" / "reliability-v3-adopted.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(installed_memory_repair.ReliabilityV3ValidationError) as raised:
        installed_memory_repair.require_reliability_v3_adopted(root=root, state_root=state_root)

    assert raised.value.code == "reliability_v3_record_invalid"
    assert raised.value.detail and raised.value.detail != raised.value.code
    assert str(raised.value).startswith("reliability_v3_record_invalid: ")
    assert raised.value.__cause__ is not None


def test_a_code_without_detail_reads_as_before() -> None:
    error = installed_memory_repair.ReliabilityV3ValidationError("legacy_protocol_unquiesced")

    assert (str(error), error.code, error.detail) == ("legacy_protocol_unquiesced", "legacy_protocol_unquiesced", "")


def test_the_adopted_queue_waits_out_a_busy_database(tmp_path: Path, monkeypatch) -> None:
    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    monkeypatch.setattr(markdown_transaction, "_ADOPTION_VALIDATION_CACHE", set())
    real = installed_memory_repair.require_reliability_v3_admission
    calls: list[int] = []

    def busy_once(*, root, state_root):
        calls.append(1)
        if len(calls) == 1:
            raise installed_memory_repair.ReliabilityV3ValidationError(
                "reliability_v3_record_invalid", "OperationalError: database is locked"
            ) from sqlite3.OperationalError("database is locked")
        return real(root=root, state_root=state_root)

    monkeypatch.setattr(installed_memory_repair, "require_reliability_v3_admission", busy_once)

    queue = memory_queue.active_memory_queue(root, state_root)

    assert queue is not None
    assert len(calls) == 2


@pytest.mark.parametrize("bounded", [False, True])
def test_admission_reads_use_existing_store_waits(tmp_path, monkeypatch, bounded):
    import time

    from reliable_memory import DEFAULTS

    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    opened = []
    real = installed_memory_repair.open_readonly_operational_db

    def observe(path, *args, **kwargs):
        database = real(path, *args, **kwargs)
        opened.append((path.name, database.execute("PRAGMA busy_timeout").fetchone()[0]))
        return database

    monkeypatch.setattr(installed_memory_repair, "open_readonly_operational_db", observe)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    deadline = 101.0 if bounded else None
    installed_memory_repair.require_reliability_v3_admission(root=root, state_root=state_root, deadline=deadline)
    waits = dict(opened)
    expected = {"queue-v3.sqlite3": DEFAULTS.queue_busy_ms,
                "markdown-transactions-v3.sqlite3": DEFAULTS.markdown_busy_ms}
    if bounded:
        assert all(0 < waits[name] <= 1000 for name in expected)
        return
    assert {name: waits[name] for name in expected} == expected


@contextmanager
def _short_exclusive_write(path):
    import subprocess
    import threading

    code = '''import sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute("BEGIN EXCLUSIVE")
print("locked",flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
'''
    with subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as writer:
        assert writer.stdout.readline().strip() == "locked"
        # Fixture's controlled transaction duration, not a product retry delay.
        release = threading.Timer(0.2, lambda: writer.communicate("release\n"))
        release.start()
        try:
            yield
        finally:
            release.join()
        assert writer.returncode == 0


@pytest.mark.parametrize("name", ["queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"])
@pytest.mark.parametrize("validate", [installed_memory_repair.require_reliability_v3_admission,
                                      installed_memory_repair.require_reliability_v3_adopted])
def test_adoption_reads_survive_a_short_exclusive_writer(tmp_path, name, validate):
    import time

    root, state_root, _home = _build_root(tmp_path)
    _adopt(root, state_root)
    with _short_exclusive_write(state_root / "run" / name):
        validate(root=root, state_root=state_root, deadline=time.monotonic() + LONG_TIMEOUT)
