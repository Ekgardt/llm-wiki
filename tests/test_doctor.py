"""The doctor report, its command line, read-only runs, filesystem health, repair and the maintenance owner."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import generation_maintenance
import pytest
from llm_wiki import runtime_environment

from tests.doctor_case_helpers import (
    _snapshot,
    _symlink_or_skip,
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    SCRIPTS,
    _adopt,
    _build_root,
    _cache_pinned_models,
    _check,
    _create_claim_index,
    _create_generation,
    _qualified_pyright_check,
)
from tests.slow_machine import LONG_TIMEOUT, SHORT_TIMEOUT
from tests.sqlite_errors import busy_database_error

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


DOCTOR = SCRIPTS / "doctor.py"


def test_a_vault_that_has_not_adopted_v3_says_capture_is_disabled_and_names_the_command(
    tmp_path,
):
    """Issue #17: every capture failed silently until the adoption was run by hand."""
    import time

    from llm_wiki import (
        doctor_capture,
    )

    root, state_root, _home = _build_root(tmp_path)

    result = doctor_capture.capture_check(root, state_root, time.monotonic() + 30)

    assert result["status"] == "degraded"
    assert "Session capture is disabled" in result["message"]
    assert "--adopt-ownership-v3" in result["message"]
    assert result["details"]["adoption_state"] in {"fresh", "upgrade-required", "conflict", "unknown"}


def _create_index(path: Path, paths: list[str] | None = None, manifest: bool = True) -> None:
    paths = paths or []
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE pages USING fts5("
        "path UNINDEXED, title, summary, body, project UNINDEXED, "
        "timestamp UNINDEXED, slug, tokenize = 'porter unicode61')"
    )
    connection.execute(
        "CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO index_metadata (key, value) VALUES ('paths', ?)",
        (json.dumps(sorted(paths), separators=(",", ":")),),
    )
    for item in paths:
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item, "title", "summary", "body", "", "", Path(item).stem),
        )
    connection.commit()
    connection.close()
    if manifest:
        (path.parent / ".paths-manifest").write_text(json.dumps(sorted(paths)), encoding="utf-8")


def test_doctor_accepts_active_graph_v2_without_code_capture(tmp_path: Path) -> None:
    import doctor
    from llm_wiki.generation_catalog_store import GenerationCatalog

    root, state_root, home = _build_root(tmp_path)
    _create_generation(root, state_root)
    manifest = GenerationCatalog(state_root)._registered_manifest("healthy-generation")
    report = doctor.run_doctor(root=root, state_root=state_root, home=home)

    assert "code_capture" not in manifest
    assert _check(report, "generation")["status"] == "ok"


def _write_lock(path: Path, *, pid: int, acquired_at: str) -> None:
    path.write_text(
        json.dumps(
            {
                "lock_pid": pid,
                "lock_token": "lock-token",
                "lock_acquired_at": acquired_at,
            }
        ),
        encoding="utf-8",
    )


def _take_snapshot(root: Path, home: Path) -> None:
    """A healthy vault has its nightly second copy: a snapshot repository with a commit."""
    from snapshot_knowledge import snapshot_root

    del root
    repository = snapshot_root(home)
    repository.mkdir(parents=True, exist_ok=True)
    identity = ["-c", "user.name=test", "-c", "user.email=test@localhost"]
    for command in (["init", "--quiet"], [*identity, "commit", "--quiet", "--allow-empty", "-m", "snapshot"]):
        subprocess.run(["git", "-C", str(repository), *command], check=True, capture_output=True)


def _check_ids(report) -> set:
    return {item["id"] for item in report["checks"]}


def _check_shapes(report) -> set:
    """Every check's status word, a non-empty message and a details mapping."""
    return {
        (
            item["status"] in {"ok", "degraded", "error", "skipped"},
            isinstance(item["message"], str) and bool(item["message"]),
            isinstance(item["details"], dict),
        )
        for item in report["checks"]
    }


def test_report_schema_and_all_check_classes_are_json_safe(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    now = datetime(2026, 7, 13, 12, tzinfo=UTC)
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": "2026-07-13", "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    _create_claim_index(root, state_root)
    _create_generation(root, state_root)
    _cache_pinned_models(tmp_path, monkeypatch)
    _take_snapshot(root, home)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, now=now)

    assert (
        report["schema_version"],
        report["generated_at"].endswith("+00:00"),
        report["overall_status"],
    ) == ("1.0", True, "ok")
    assert _check_ids(report) == {
        "environment",
        "runtime",
        "adoption",
        "filesystem",
        "transactions",
        "queue",
        "archives",
        "claims",
        "generation",
        "scheduler",
        "capture",
        "tools",
        "backup",
        "models",
        "settings",
        "hooks",
        "checkpoints",
        "mcp",
        "integrations",
        "pyright",
        "lsp",
        "run_deletion",
    }
    assert (
        set(report["counts"]),
        "summary" in report,
        report["repaired"],
        _check_shapes(report),
    ) == ({"ok", "degraded", "error", "skipped"}, False, [], {(True, True, True)})
    json.dumps(report, allow_nan=False)


def test_environment_reports_missing_root_layout_and_python(tmp_path, monkeypatch):
    import doctor

    root = tmp_path / "missing"
    monkeypatch.setattr(sys, "version_info", (3, 9, 0))

    check = _check(doctor.run_doctor(root=root, state_root=tmp_path / "state"), "environment")

    assert check["status"] == "error"
    assert check["details"]["python"]["status"] == "error"
    assert check["details"]["vault_root"]["status"] == "error"


def test_the_cli_accepts_a_larger_time_budget_and_refuses_an_impossible_one(
    tmp_path,
    monkeypatch,
    capsys,
):
    """A slow machine must be able to ask for more time than the five-second default."""
    import doctor

    root, state_root, home = _build_root(tmp_path)
    seen = {}

    def record(**kwargs):
        seen.update(kwargs)
        return {"overall_status": "ok", "repaired": [], "checks": [], "scope": kwargs["scope"]}

    monkeypatch.setattr(doctor, "run_doctor", record)

    asked = (doctor.main(["--time-budget", "45"]), seen["time_budget_seconds"])
    defaulted = (doctor.main([]), seen["time_budget_seconds"])

    assert (asked, defaulted) == (
        (0, 45.0),
        (0, generation_maintenance.DEFAULT_TIME_BUDGET_SECONDS),
    )
    assert seen["scope"] == "full"
    assert doctor.main(["--scope", "operational"]) == 0
    assert seen["scope"] == "operational"
    with pytest.raises(SystemExit):
        doctor.main(["--time-budget", "0"])
    assert "positive" in capsys.readouterr().err
    del root, state_root, home


def test_read_only_runtime_probe_leaves_no_files_or_directories(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    before = _snapshot(tmp_path)

    report = run_doctor(root=root, state_root=state_root, home=home)

    assert _snapshot(tmp_path) == before
    assert _check(report, "runtime")["status"] == "ok"
    assert not list(state_root.rglob("*.doctor-probe*"))


def _whole_tree(path: Path) -> dict[str, bytes | None]:
    """Every entry under `path`, hidden ones and directories included."""
    return {
        item.relative_to(path).as_posix(): item.read_bytes() if item.is_file() else None
        for item in path.rglob("*")
    }


def test_a_read_only_run_leaves_every_entry_as_it_was(tmp_path):
    """The locking probe is the one file doctor touches, and it is gone again (audit 2026-09-27 C-1)."""
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    before = _whole_tree(tmp_path)

    run_doctor(root=root, state_root=state_root, home=home)

    assert _whole_tree(tmp_path) == before


def test_read_only_run_writes_through_no_python_file_api(tmp_path, monkeypatch):
    """Python-level writes are refused; the SQLite locking probe writes below this layer by design."""
    import doctor

    root, state_root, home = _build_root(tmp_path)

    real_open = os.open

    def reject_write(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags:
            raise AssertionError("read-only doctor attempted a write")
        return real_open(path, flags, *args, **kwargs)

    def reject_path_write(*args, **kwargs):
        raise AssertionError("read-only doctor attempted a write")

    monkeypatch.setattr(os, "open", reject_write)
    monkeypatch.setattr(Path, "write_text", reject_path_write)
    monkeypatch.setattr(Path, "touch", reject_path_write)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home)

    assert _check(report, "runtime")["status"] == "ok"


@pytest.mark.parametrize(
    ("system", "mount_data", "darwin_data", "state_path"),
    [
        (
            "Linux",
            "server:/share /mnt/wiki nfs4 rw 0 0\n",
            "",
            "/mnt/wiki/state",
        ),
        (
            "Darwin",
            "",
            "//server/share on /Volumes/Wiki (smbfs, nodev)\n",
            "/Volumes/Wiki/state",
        ),
        ("Windows", "", "", r"Z:\\wiki-state"),
    ],
)
def test_filesystem_health_rejects_network_mounts_on_supported_platforms(
    tmp_path, monkeypatch, system, mount_data, darwin_data, state_path
):
    import reliable_memory

    monkeypatch.setattr(runtime_environment, "platform_system", lambda: system)
    monkeypatch.setattr(runtime_environment, "read_posix_mount_data", lambda: (mount_data, False))
    monkeypatch.setattr(runtime_environment, "query_darwin_mounts", lambda: darwin_data)
    if system != "Windows":
        monkeypatch.setattr(type(Path("/")), "resolve", lambda self, *, strict=False: self)
    if system == "Windows":
        monkeypatch.setattr(
            reliable_memory.ctypes,
            "windll",
            type(
                "Windll",
                (),
                {"kernel32": type("Kernel32", (), {"GetDriveTypeW": lambda self, anchor: 4})()},
            )(),
            raising=False,
        )
    monkeypatch.setattr(
        reliable_memory,
        "sqlite_lock_probe",
        lambda *args, **kwargs: pytest.fail("lock probe ran on network storage"),
    )

    check = generation_maintenance.filesystem_check(Path(state_path))

    assert check["status"] == "error"
    assert check["details"] == {"local": False, "locking": "unsupported"}


def test_filesystem_health_distinguishes_broken_and_unavailable_lock_probe(tmp_path, monkeypatch):
    import reliable_memory

    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(reliable_memory, "known_network_path", lambda path: False)
    monkeypatch.setattr(reliable_memory, "sqlite_lock_probe", lambda path, **kwargs: False)
    broken = generation_maintenance.filesystem_check(state_root)
    monkeypatch.setattr(reliable_memory, "sqlite_lock_probe", lambda path, **kwargs: None)
    unavailable = generation_maintenance.filesystem_check(state_root)

    assert broken["status"] == "error"
    assert broken["details"] == {"local": True, "locking": "unsupported"}
    assert unavailable["status"] == "degraded"
    assert unavailable["details"] == {"local": True, "locking": "unknown"}


def test_filesystem_health_runs_bounded_probe_and_leaves_no_artifacts(tmp_path, monkeypatch):
    import reliable_memory

    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "run").mkdir()
    before = _snapshot(tmp_path)
    calls = []
    real_probe = reliable_memory.sqlite_lock_probe

    def probe(path, *, deadline):
        calls.append((path, deadline))
        return real_probe(path, deadline=deadline)

    monkeypatch.setattr(reliable_memory, "known_network_path", lambda path: False)
    monkeypatch.setattr(reliable_memory, "sqlite_lock_probe", probe)

    check = generation_maintenance.filesystem_check(state_root, deadline=time.monotonic() + SHORT_TIMEOUT)

    assert (
        check["status"],
        bool(calls) and calls[0][0] == state_root,
        calls[0][1] != float("inf"),
        _snapshot(tmp_path) == before,
        list(state_root.rglob(".llm-wiki-lock-probe-*")),
    ) == ("ok", True, True, True, [])


def test_read_only_missing_runtime_does_not_create_it(tmp_path):
    from doctor import run_doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "absent-state"

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "runtime")
    environment = _check(run_doctor(root=root, state_root=state_root, home=home), "environment")

    assert check["status"] == "degraded"
    assert environment["details"]["state_root"]["status"] == "error"
    assert not state_root.exists()


def test_queue_counts_retained_json_records_without_reading_them(tmp_path):
    """The JSON queue of v3.3.0–v3.4.0 is counted and named, never read (2026-09-23)."""
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    queue = state_root / "run" / "queue"
    queue.mkdir()
    secret = "TOP-SECRET-QUEUE-PAYLOAD"
    (queue / "pending.json").write_text(
        json.dumps({"attempts": 1, "payload": {"prompt": secret}}), encoding="utf-8"
    )
    (queue / "stuck.processing").write_text(secret, encoding="utf-8")

    report = run_doctor(root=root, state_root=state_root, home=home)
    check = _check(report, "queue")

    assert (
        check["status"],
        check["details"]["legacy_retained"],
        "legacy_queue_retained" in check["details"]["deletion_codes"],
        "run/queue" in check["message"],
        secret in json.dumps(report),
    ) == ("degraded", 2, True, True, False)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "skipped"),
        ({"last_nightly_date": "2026-07-13", "last_nightly_status": "success"}, "ok"),
        (
            {
                "last_nightly_date": "2026-07-10",
                "last_nightly_status": "success",
                "last_nightly_skip": {
                    "skipped_at": "2026-07-13T03:00:00",
                    "reason": "maintenance_lock_held",
                },
            },
            "degraded",
        ),
        ({"last_nightly_status": "failed"}, "error"),
    ],
)
def test_scheduler_uses_local_state_only(tmp_path, state, expected):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    (state_root / "run" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    check = _check(
        run_doctor(
            root=root,
            state_root=state_root,
            home=home,
            now=datetime(2026, 7, 13, tzinfo=UTC),
        ),
        "scheduler",
    )
    assert check["status"] == expected


def test_mcp_is_optional_but_reports_source_and_capability(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "mcp")

    assert check["status"] == "ok"
    assert check["details"]["package"] == "skipped"
    assert check["details"]["core_capture_required"] is False


def _runtime_directories_exist(state_root) -> bool:
    present = {str(path) for path in state_root.rglob("*") if path.is_dir()}
    return {str(state_root / item) for item in ("run", "logs", "cache")} <= present


def _did(report, action: str) -> bool:
    return any(item["action"] == action for item in report["repaired"])


def test_repair_creates_runtime_and_is_idempotent(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "new-state"

    first = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    after_first = _snapshot(state_root)
    second = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert (
        _runtime_directories_exist(state_root),
        _did(first, "create_runtime_directory"),
        second["repaired"],
        set(_snapshot(state_root)) == set(after_first),
    ) == (True, True, [], True)


def test_maintenance_owner_is_exclusive_heartbeated_released_and_fenced(tmp_path):

    root, state_root, _ = _build_root(tmp_path)
    now = datetime.now(UTC)

    first = generation_maintenance.acquire_maintenance_owner(root, state_root, now)
    assert first is not None
    coordinator, lease = first
    assert generation_maintenance.acquire_maintenance_owner(root, state_root, now) is None

    generation_maintenance.heartbeat_maintenance_owner(coordinator, lease, now + timedelta(seconds=1))
    generation_maintenance.release_maintenance_owner(coordinator, lease)
    second = generation_maintenance.acquire_maintenance_owner(root, state_root, now + timedelta(seconds=2))

    assert second is not None
    second_coordinator, second_lease = second
    assert second_lease["epoch"] == lease["epoch"] + 1
    generation_maintenance.release_maintenance_owner(second_coordinator, second_lease)


def test_live_pid_prevents_expired_maintenance_owner_reclaim(tmp_path, monkeypatch):

    root, state_root, _ = _build_root(tmp_path)
    now = datetime.now(UTC)
    first = generation_maintenance.acquire_maintenance_owner(root, state_root, now)
    assert first is not None
    coordinator, lease = first
    with coordinator._connect() as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE owner_name='doctor'",
            ((now - timedelta(minutes=1)).isoformat(),),
        )
        database.commit()

    assert generation_maintenance.acquire_maintenance_owner(root, state_root, now) is None
    generation_maintenance.release_maintenance_owner(coordinator, lease)


def test_a_lost_fence_names_the_check_and_the_owner_it_found(tmp_path):
    """A deferred nightly rebuild must say who took the fence, not just that it went."""

    root, state_root, _ = _build_root(tmp_path)
    now = datetime.now(UTC)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state_root, now)
    assert acquired is not None
    coordinator, lease = acquired
    with coordinator._connect() as database:
        database.execute(
            "UPDATE maintenance_owners SET owner_token='thief',process_id=?,"
            "fencing_epoch=? WHERE owner_name='doctor'",
            (os.getpid() + 1, int(lease["epoch"]) + 1),
        )
        database.commit()

    with pytest.raises(generation_maintenance.MaintenanceFenceLost) as required:
        generation_maintenance.require_maintenance_owner(coordinator, lease)
    with pytest.raises(generation_maintenance.MaintenanceFenceLost) as beaten:
        generation_maintenance.heartbeat_maintenance_owner(coordinator, lease)

    observed = required.value.observed
    assert (
        str(required.value),
        required.value.where,
        beaten.value.where,
        generation_maintenance.fence_loss_details(required.value)["where"],
    ) == ("maintenance_owner_fence_lost", "require", "heartbeat", "require")
    assert (
        observed["process_id"],
        observed["fencing_epoch"],
        observed["held_epoch"],
    ) == (os.getpid() + 1, int(lease["epoch"]) + 1, lease["epoch"])


def test_maintenance_heartbeat_runs_during_long_operation(tmp_path, monkeypatch):

    root, state_root, _ = _build_root(tmp_path)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state_root, datetime.now(UTC))
    assert acquired is not None
    coordinator, lease = acquired
    beats = []
    second_beat = threading.Event()
    real = generation_maintenance.heartbeat_maintenance_owner

    def heartbeat(*args, **kwargs):
        result = real(*args, **kwargs)
        beats.append(time.monotonic())
        if len(beats) >= 2:
            second_beat.set()
        return result

    monkeypatch.setattr(generation_maintenance, "MAINTENANCE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(generation_maintenance, "heartbeat_maintenance_owner", heartbeat)

    def wait_for_two_heartbeats() -> None:
        # Each beat writes through the coordinator, so two of them cost far
        # more than the 10 ms interval on a loaded machine. The wait ends as
        # soon as the second beat lands; the budget is only its upper bound.
        assert second_beat.wait(timeout=LONG_TIMEOUT)

    with generation_maintenance.MaintenanceHeartbeat(
        coordinator, lease, deadline=time.monotonic() + LONG_TIMEOUT
    ) as guard:
        guard.run(wait_for_two_heartbeats)

    assert len(beats) >= 2


def test_a_busy_database_is_not_a_lost_fence(tmp_path, monkeypatch):
    """A locked database threw away seven-minute generation builds on this vault.

    The lease outlives two missed beats, so a transient failure is retried; a
    fence genuinely taken by someone else still ends the pass at once.
    """

    root, state_root, _ = _build_root(tmp_path)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state_root, datetime.now(UTC))
    assert acquired is not None
    coordinator, lease = acquired
    guard = generation_maintenance.MaintenanceHeartbeat(
        coordinator, lease, deadline=time.monotonic() + LONG_TIMEOUT
    )

    monkeypatch.setattr(
        generation_maintenance,
        "heartbeat_maintenance_owner",
        lambda *args, **kwargs: (_ for _ in ()).throw(busy_database_error()),
    )
    fence_lost = RuntimeError("maintenance_owner_fence_lost")
    busy = generation_maintenance.transient_beat_failure(busy_database_error())
    assert (busy, generation_maintenance.transient_beat_failure(fence_lost), guard.cancelled()) == (
        True,
        False,
        False,
    )
    guard._lost.set()
    assert guard.cancelled() is True

    generation_maintenance.release_maintenance_owner(coordinator, lease)


def test_repair_never_mutates_personal_markdown(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    personal = root / "knowledge" / "notes" / "private.md"
    personal.write_text("private durable knowledge", encoding="utf-8")
    before = personal.read_bytes()

    doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert personal.read_bytes() == before


def test_degraded_summary_is_empty_for_healthy_and_bounded(tmp_path):
    from doctor import degraded_summary, run_doctor

    root, state_root, home = _build_root(tmp_path)
    healthy = {
        "overall_status": "ok",
        "checks": [{"id": "runtime", "status": "ok", "message": "healthy", "details": {}}],
    }
    assert degraded_summary(healthy) == ""

    report = run_doctor(root=root, state_root=state_root, home=home)
    summary = degraded_summary(report)
    assert "generation" in summary
    assert "scheduler" not in summary
    assert len(summary) <= 600


@pytest.mark.parametrize(("setup", "exit_code"), [("degraded", 1), ("error", 2)])
def test_cli_json_exit_codes(tmp_path, setup, exit_code):
    root, state_root, home = _build_root(tmp_path)
    if setup == "error":
        root = tmp_path / "missing-vault"
    env = os.environ.copy()
    env.update(
        LLM_WIKI_ROOT=str(root),
        LLM_WIKI_STATE_ROOT=str(state_root),
        HOME=str(home),
        USERPROFILE=str(home),
    )

    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == exit_code
    assert report["overall_status"] == setup


def test_cli_returns_zero_for_healthy_report(tmp_path, monkeypatch, capsys):
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    _adopt(root, state_root)
    today = datetime.now(UTC).date().isoformat()
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": today, "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    _create_index(state_root / "cache" / "index.sqlite")
    _create_claim_index(root, state_root)
    _create_generation(root, state_root)
    _cache_pinned_models(tmp_path, monkeypatch)
    _take_snapshot(root, home)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)

    return_code = doctor.main(["--json"])
    report = json.loads(capsys.readouterr().out)

    assert return_code == 0
    assert report["overall_status"] == "ok"


def test_import_with_missing_state_root_creates_nothing(tmp_path):
    state_root = tmp_path / "missing-state"
    env = os.environ.copy()
    env["LLM_WIKI_STATE_ROOT"] = str(state_root)
    code = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import doctor"

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not state_root.exists()


def _check_statuses(report: dict) -> dict[str, str]:
    return {check["id"]: check["status"] for check in report["checks"]}


def test_cli_repair_json_is_idempotent(tmp_path, monkeypatch, capsys):
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, _, home = _build_root(tmp_path)
    # Adopted, so capture is enabled (issue #17); everything else under the
    # state root is still missing and is what `--repair` creates.
    state_root = tmp_path / "missing-state"
    _adopt(root, state_root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    clock = iter([0.0])
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock, 6.0))
    budgeted = ["--repair", "--json", "--time-budget", "30"]

    first_return_code = doctor.main(budgeted)
    first_report = json.loads(capsys.readouterr().out)
    after_first = _snapshot(state_root)
    second_return_code = doctor.main(budgeted)
    second_report = json.loads(capsys.readouterr().out)

    # A fresh vault has no generation until the installer's sync or the nightly
    # builds one (2026-09-23): `--repair` leaves that one honest degradation and
    # names it, and the second run finds nothing left to repair.
    statuses = _check_statuses(first_report)
    degraded = {check_id for check_id, status in statuses.items() if status != "ok"}
    allowed = {"generation", "models", "scheduler", "backup"}
    assert (bool(first_report["repaired"]), degraded <= allowed, "error" in statuses.values()) == (True, True, False)
    assert (
        second_return_code,
        second_report["overall_status"],
        second_report["repaired"],
        set(_snapshot(state_root)) == set(after_first),
    ) == (first_return_code, first_report["overall_status"], [], True)


def test_repair_does_not_touch_knowledge_config_network_or_subprocess(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    knowledge = root / "knowledge" / "notes" / "private.md"
    knowledge.write_text("private durable knowledge", encoding="utf-8")
    config_dir = home / ".claude"
    config_dir.mkdir()
    config = config_dir / "settings.json"
    config.write_text('{"secret": "local"}', encoding="utf-8")
    before_root = _snapshot(root)
    before_home = _snapshot(home)

    def reject_external(*args, **kwargs):
        raise AssertionError("repair attempted an external operation")

    monkeypatch.setattr(urllib.request, "urlopen", reject_external)
    monkeypatch.setattr(socket, "create_connection", reject_external)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert report["overall_status"] in {"ok", "degraded"}, report
    assert _snapshot(root) == before_root
    assert _snapshot(home) == before_home


def test_repair_rejects_symlinked_runtime_component(tmp_path):

    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    external = tmp_path / "external-logs"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("safe", encoding="utf-8")
    shutil.rmtree(state_root / "logs")
    _symlink_or_skip(state_root / "logs", external, directory=True)

    report = run_doctor(root=root, state_root=state_root, home=home, repair=True)

    runtime = _check(report, "runtime")
    assert runtime["status"] == "error"
    assert runtime["details"]["logs"]["symlink"] is True
    assert sentinel.read_text(encoding="utf-8") == "safe"


def test_queue_ignores_symlink_entries_without_following_payload(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    external = tmp_path / "outside.json"
    external.write_text('{"payload":"OUTSIDE-SECRET"}', encoding="utf-8")
    (state_root / "run" / "queue").mkdir()
    link = state_root / "run" / "queue" / "linked.json"
    _symlink_or_skip(link, external)

    report = run_doctor(root=root, state_root=state_root, home=home)
    queue = _check(report, "queue")

    assert queue["status"] == "error"
    assert queue["details"]["artifact_error"] is True
    assert queue["details"]["legacy_retained"] == 1
    assert "OUTSIDE-SECRET" not in json.dumps(report)


def test_queue_scan_is_bounded_by_count_and_file_size(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    queue = state_root / "run" / "queue"
    queue.mkdir()
    for number in range(250):
        (queue / f"{number:04}.json").write_bytes(b"x" * 70_000)

    started = time.perf_counter()
    report = run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=0.1,
    )
    elapsed = time.perf_counter() - started
    details = _check(report, "queue")["details"]

    assert (
        elapsed < 0.5,
        details["artifact_truncated"],
        details["legacy_retained"] <= 200,
        _check(report, "queue")["status"],
    ) == (True, True, True, "degraded")


def test_oversized_state_is_bounded_and_reported(tmp_path):
    from doctor import (
        run_doctor,
    )
    from llm_wiki.doctor_generation import (
        MAX_STATE_BYTES,
    )

    root, state_root, home = _build_root(tmp_path)
    (state_root / "run" / "state.json").write_bytes(b"x" * (MAX_STATE_BYTES + 1))

    report = run_doctor(root=root, state_root=state_root, home=home)
    scheduler = _check(report, "scheduler")

    assert scheduler["status"] == "degraded"
    assert scheduler["details"]["state_error"] == "oversized"


def test_run_doctor_uses_supplied_absolute_deadline_after_queue_delay(tmp_path, monkeypatch):
    import doctor
    from llm_wiki import (
        doctor_base,
    )

    root = tmp_path / "vault"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    now = [40.0]
    captured = []

    def monotonic():
        return now[0]

    def delayed_environment(*args, **kwargs):
        now[0] = 49.0
        return generation_maintenance.result("environment", "ok", "ok", {})

    def filesystem(_state_root, deadline, **kwargs):
        captured.append(deadline)
        raise RuntimeError("stop after deadline capture")

    monkeypatch.setattr(doctor.time, "monotonic", monotonic)
    monkeypatch.setattr(doctor_base, "environment_check", delayed_environment)
    monkeypatch.setattr(
        doctor_base,
        "runtime_check",
        lambda *args, **kwargs: generation_maintenance.result("runtime", "ok", "ok", {}),
    )
    monkeypatch.setattr(generation_maintenance, "filesystem_check", filesystem)

    report = doctor.run_doctor(root=root, state_root=state, deadline=50.0)

    filesystem_check = next(check for check in report["checks"] if check["id"] == "filesystem")
    assert (captured, filesystem_check["message"]) == (
        [50.0],
        "The filesystem check could not finish: RuntimeError: stop after deadline capture",
    )


def test_budget_exhaustion_degrades_overall_and_health_summary(tmp_path):
    from doctor import degraded_summary, run_doctor

    root, state_root, home = _build_root(tmp_path)

    report = run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=0,
    )
    generation = _check(report, "generation")

    assert generation["status"] == "degraded"
    assert generation["details"]["budget_exhausted"] is True
    assert report["overall_status"] != "ok"
    assert degraded_summary(report)


def test_the_read_wait_survives_a_budgetless_deadline():
    """Most checks default to an infinite deadline; arithmetic must survive it."""
    from llm_wiki import doctor_base

    assert doctor_base.read_busy_ms(float("inf")) == doctor_base.READ_BUSY_MS
    assert doctor_base.read_busy_ms(None) == doctor_base.READ_BUSY_MS
    assert doctor_base.read_busy_ms(time.monotonic() - 1) == 0
    assert 0 < doctor_base.read_busy_ms(time.monotonic() + 0.1) <= doctor_base.READ_BUSY_MS


def test_unrelated_installed_configs_are_not_false_positives(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    configs = {
        home / ".claude" / "settings.json": "unrelated claude config",
        home / ".config" / "opencode" / "plugins" / "llm-wiki-memory.js": "unrelated plugin",
        home / ".codex" / "config.toml": "unrelated codex config",
    }
    for path, content in configs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    hosts = check["details"]["hosts"]
    assert (
        check["status"],
        hosts["claude"]["status"],
        hosts["opencode"]["status"],
        hosts["codex"]["status"],
    ) == ("degraded", "degraded", "degraded", "degraded")


def test_no_nested_part_takes_the_scripts_directory_for_the_checkout():
    """A part moved from scripts/ into scripts/llm_wiki/ is one directory deeper."""
    from tests.script_sources import SCRIPTS

    one_level_up = re.compile(r"__file__\)(\.resolve\(\))?\.parent\.parent\b")
    offenders = [
        path.name
        for path in sorted((SCRIPTS / "llm_wiki").glob("*.py"))
        if one_level_up.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
