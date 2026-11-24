"""The guard that stops a test from writing into the owner's live vault.

This checkout has been the vault itself since the two directories merged on
2026-08-21, so a test that writes knowledge through the pinned `LLM_WIKI_ROOT`
writes into the owner's memory. `tests/conftest.py` snapshots the watched
directories around the session and fails the run when anything changed.

Session records were left out of that watch, and it cost three fixture files:
`knowledge/raw/sessions/2026-08-2{4,5,6}/session-1.md`, written by
`test_flush_classification` while only `flush_memory.STATE_ROOT` was patched.
The writer never raises by contract, so nothing reported the leak. These tests
hold the watch in place.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests import conftest
from tests.slow_machine import LONG_TIMEOUT


def _posix(entries: list[str]) -> list[str]:
    """The guard reports paths with the platform's separators (`conftest`
    builds them with `str(path.relative_to(root))`), so on Windows they carry
    backslashes. These tests state expectations in POSIX form and normalize
    the reported side, keeping the guard itself untouched."""
    return [Path(entry).as_posix() for entry in entries]


def _write(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("leaked\n", encoding="utf-8")
    return path


def test_a_session_record_written_during_a_run_is_reported(tmp_path):
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/raw/sessions/2026-08-26/session-1.md")

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert _posix(leaked) == ["knowledge/raw/sessions/2026-08-26/session-1.md"]


def test_a_session_record_that_was_already_there_is_not_reported(tmp_path):
    _write(tmp_path, "knowledge/raw/sessions/2026-08-26/session-1.md")
    before = conftest._knowledge_entries(tmp_path)

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert leaked == []


def test_the_watch_still_covers_notes_and_projects(tmp_path):
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/notes/leaked-page.md")
    _write(tmp_path, "knowledge/projects/leaked-project/journal.md")

    leaked = conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))

    assert _posix(leaked) == [
        "knowledge/notes/leaked-page.md",
        "knowledge/projects/leaked-project",
    ]


def _daily_leak(tmp_path, monkeypatch, host_vault) -> list:
    monkeypatch.setattr(conftest, "_HOST_VAULT", host_vault)
    before = conftest._knowledge_entries(tmp_path)
    _write(tmp_path, "knowledge/daily/2026-08-26.md")
    return conftest._leaked_entries(before, conftest._knowledge_entries(tmp_path))


def test_the_daily_log_of_the_live_vault_is_not_watched(tmp_path, monkeypatch):
    """The live capture appends to today's log continuously; that is not a leak."""
    assert _daily_leak(tmp_path, monkeypatch, str(tmp_path)) == []


def test_the_daily_log_of_any_other_checkout_is_watched(tmp_path, monkeypatch):
    """Nothing but a test appends there (docs/research/2026-09-25-a-test-that-writes-a-daily-log-is-caught.md)."""
    assert _daily_leak(tmp_path, monkeypatch, None) == [str(Path("knowledge/daily/2026-08-26.md"))]


def test_the_host_vault_is_read_before_the_checkout_pins_it():
    environ = {"LLM_WIKI_ROOT": "/live-vault"}

    assert conftest._host_vault(environ) == "/live-vault"
    assert environ[conftest.HOST_VAULT_VARIABLE] == "/live-vault"


def test_a_worker_inherits_the_host_vault_not_the_pinned_checkout():
    """xdist workers inherit the controller's pinned `LLM_WIKI_ROOT`; the recorded host value wins."""
    environ = {"LLM_WIKI_ROOT": str(conftest.VAULT_ROOT), conftest.HOST_VAULT_VARIABLE: ""}

    assert conftest._host_vault(environ) is None


_CONTROLLER = (
    "import subprocess, sys\n"
    "from tests import conftest\n"
    "worker = [sys.executable, '-c', 'from tests import conftest; print(conftest._HOST_VAULT)']\n"
    "print(subprocess.run(worker, capture_output=True, text=True, check=True).stdout.strip())\n"
)


def test_a_child_of_a_pytest_process_still_watches_the_daily_logs(tmp_path):
    """The leak guard must not switch itself off in the processes pytest starts."""
    hidden = {"LLM_WIKI_ROOT", conftest.HOST_VAULT_VARIABLE, "PYTEST_XDIST_WORKER"}
    environ = {name: value for name, value in os.environ.items() if name not in hidden}
    environ.update(LLM_WIKI_TEST_USE_EXTERNAL_STATE="1", LLM_WIKI_STATE_ROOT=str(tmp_path / "state"))

    child = subprocess.run(
        [sys.executable, "-c", _CONTROLLER],
        cwd=conftest.VAULT_ROOT,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
        timeout=LONG_TIMEOUT,
    )

    assert child.stdout.strip() == "None"
