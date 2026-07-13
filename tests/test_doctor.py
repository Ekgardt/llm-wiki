from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
DOCTOR = SCRIPTS / "doctor.py"


def _create_index(path: Path, paths: list[str] | None = None, manifest: bool = False) -> None:
    paths = paths or []
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE pages USING fts5("
        "path UNINDEXED, title, summary, body, project UNINDEXED, "
        "timestamp UNINDEXED, slug)"
    )
    for item in paths:
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item, "title", "summary", "body", "", "", Path(item).stem),
        )
    connection.commit()
    connection.close()
    if manifest:
        (path.parent / ".paths-manifest").write_text(
            json.dumps(sorted(paths)), encoding="utf-8"
        )


def _write_lease(path: Path, *, pid: int | None, acquired_at: str) -> None:
    task = {
        "id": path.stem,
        "attempts": 0,
        "payload": {"secret": "never report"},
        "lease_acquired_at": acquired_at,
    }
    if pid is not None:
        task.update(lease_pid=pid, lease_token="token-123")
    path.write_text(json.dumps(task), encoding="utf-8")


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


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _build_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    home = tmp_path / "home"
    for path in (
        root / "knowledge" / "notes",
        root / "scripts",
        root / "integrations" / "claude-code",
        root / "integrations" / "cursor" / "rules",
        root / "integrations" / "antigravity",
        state_root / "run" / "queue",
        state_root / "logs",
        state_root / "cache",
        home,
    ):
        path.mkdir(parents=True, exist_ok=True)
    for relative in (
        "scripts/scheduled_nightly.py",
        "scripts/search_memory.py",
        "scripts/mcp_server.py",
        "scripts/llm-wiki-memory-opencode.js",
        "scripts/codex_memory.py",
        "integrations/claude-code/settings.json",
        "integrations/cursor/rules/llm-wiki.mdc",
        "integrations/antigravity/AGENTS.md",
    ):
        (root / relative).write_text("{}\n", encoding="utf-8")
    return root, state_root, home


def _check(report: dict, check_id: str) -> dict:
    return next(item for item in report["checks"] if item["id"] == check_id)


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_report_schema_and_all_check_classes_are_json_safe(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": "2026-07-13", "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    os.utime(index, (now.timestamp(), now.timestamp()))

    report = run_doctor(root=root, state_root=state_root, home=home, now=now)

    assert report["schema_version"] == "1.0"
    assert report["generated_at"].endswith("+00:00")
    assert report["overall_status"] == "ok"
    assert {item["id"] for item in report["checks"]} == {
        "environment", "runtime", "queue", "index", "scheduler", "mcp", "integrations"
    }
    assert set(report["counts"]) == {"ok", "degraded", "error", "skipped"}
    assert "summary" not in report
    assert report["repaired"] == []
    for item in report["checks"]:
        assert item["status"] in {"ok", "degraded", "error", "skipped"}
        assert isinstance(item["message"], str) and item["message"]
        assert isinstance(item["details"], dict)
    json.dumps(report, allow_nan=False)


def test_environment_reports_missing_root_layout_and_python(tmp_path, monkeypatch):
    import doctor

    root = tmp_path / "missing"
    monkeypatch.setattr(doctor.sys, "version_info", (3, 9, 0))

    check = _check(doctor.run_doctor(root=root, state_root=tmp_path / "state"), "environment")

    assert check["status"] == "error"
    assert check["details"]["python"]["status"] == "error"
    assert check["details"]["vault_root"]["status"] == "error"


def test_read_only_runtime_probe_leaves_no_files_or_directories(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    before = _snapshot(tmp_path)

    report = run_doctor(root=root, state_root=state_root, home=home)

    assert _snapshot(tmp_path) == before
    assert _check(report, "runtime")["status"] == "ok"
    assert not list(state_root.rglob("*.doctor-probe*"))


def test_read_only_run_never_attempts_a_write(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)

    def reject_write(*args, **kwargs):
        raise AssertionError("read-only doctor attempted a write")

    monkeypatch.setattr(doctor.os, "open", reject_write)
    monkeypatch.setattr(Path, "write_text", reject_write)
    monkeypatch.setattr(Path, "touch", reject_write)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home)

    assert _check(report, "runtime")["status"] == "ok"


def test_read_only_missing_runtime_does_not_create_it(tmp_path):
    from doctor import run_doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "absent-state"

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "runtime")
    environment = _check(
        run_doctor(root=root, state_root=state_root, home=home), "environment"
    )

    assert check["status"] == "degraded"
    assert environment["details"]["state_root"]["status"] == "error"
    assert not state_root.exists()


def test_queue_reports_counts_without_payloads(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    queue = state_root / "run" / "queue"
    secret = "TOP-SECRET-QUEUE-PAYLOAD"
    (queue / "pending.json").write_text(
        json.dumps({"attempts": 1, "payload": {"prompt": secret}}), encoding="utf-8"
    )
    (queue / "failed.json").write_text(
        json.dumps({"attempts": 5, "payload": {"prompt": secret}}), encoding="utf-8"
    )
    lease = queue / "stuck.processing"
    lease.write_text(secret, encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    os.utime(lease, (old.timestamp(), old.timestamp()))

    report = run_doctor(root=root, state_root=state_root, home=home)
    check = _check(report, "queue")

    assert check["status"] == "error"
    assert check["details"]["pending"] == 2
    assert check["details"]["permanently_failed"] == 1
    assert check["details"]["stale_leases"] == 1
    assert secret not in json.dumps(report)


def test_index_missing_stale_and_fresh_states(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    assert _check(run_doctor(root=root, state_root=state_root, home=home, now=now), "index")["status"] == "degraded"

    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    old = now - timedelta(days=2)
    os.utime(index, (old.timestamp(), old.timestamp()))
    stale = _check(run_doctor(root=root, state_root=state_root, home=home, now=now), "index")
    assert stale["status"] == "degraded"
    assert stale["details"]["freshness"] == "stale"

    os.utime(index, (now.timestamp(), now.timestamp()))
    fresh = _check(run_doctor(root=root, state_root=state_root, home=home, now=now), "index")
    assert fresh["status"] == "ok"
    assert fresh["details"]["freshness"] == "fresh"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, "skipped"),
        ({"last_nightly_date": "2026-07-13", "last_nightly_status": "success"}, "ok"),
        ({
            "last_nightly_date": "2026-07-10",
            "last_nightly_status": "success",
            "last_nightly_skip": {
                "skipped_at": "2026-07-13T03:00:00",
                "reason": "maintenance_lock_held",
            },
        }, "degraded"),
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
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        ),
        "scheduler",
    )
    assert check["status"] == expected


def test_mcp_is_optional_but_reports_source_and_capability(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "mcp")

    assert check["status"] == "ok"
    assert check["details"]["package"] == "skipped"
    assert check["details"]["core_capture_required"] is False


def test_integrations_use_injected_home_and_skip_absent_optional_hosts(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        "LLM_WIKI_ROOT session_start_context.py", encoding="utf-8"
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "ok"
    assert check["details"]["hosts"]["claude"]["status"] == "ok"
    assert check["details"]["hosts"]["opencode"]["status"] == "skipped"
    assert str(home) not in json.dumps(check)


def test_repair_creates_runtime_and_is_idempotent(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "new-state"
    rebuilt = []

    def rebuild(root, state):
        rebuilt.append(state)
        _create_index(state / "cache" / "index.sqlite")

    monkeypatch.setattr(doctor, "_rebuild_index", rebuild)

    first = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    after_first = _snapshot(state_root)
    second = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert {str(state_root / item) for item in ("run", "run/queue", "logs", "cache")} <= {
        str(path) for path in state_root.rglob("*") if path.is_dir()
    }
    assert any(item["action"] == "create_runtime_directory" for item in first["repaired"])
    assert second["repaired"] == []
    assert _snapshot(state_root) == after_first
    assert len(rebuilt) == 1


def test_ownerless_stale_lease_is_degraded_and_not_repaired(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    lease = state_root / "run" / "queue" / "legacy.processing"
    _write_lease(
        lease,
        pid=None,
        acquired_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    )

    report = run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert lease.exists()
    assert not lease.with_suffix(".json").exists()
    assert _check(report, "queue")["details"]["ownerless_leases"] == 1


def test_stale_lease_with_active_owner_is_never_recovered(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    lease = state_root / "run" / "queue" / "active.processing"
    _write_lease(
        lease,
        pid=os.getpid(),
        acquired_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    )

    run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert lease.exists()
    assert not lease.with_suffix(".json").exists()


def test_stale_lease_with_dead_owner_is_recovered(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    lease = state_root / "run" / "queue" / "dead.processing"
    _write_lease(
        lease,
        pid=999999,
        acquired_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert not lease.exists()
    assert lease.with_suffix(".json").exists()
    assert any(item["action"] == "recover_stale_lease" for item in report["repaired"])


def test_lease_recovery_never_clobbers_target_appearing_concurrently(
    tmp_path, monkeypatch
):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    lease = state_root / "run" / "queue" / "race.processing"
    target = lease.with_suffix(".json")
    _write_lease(
        lease,
        pid=999999,
        acquired_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    def target_appears(source, destination, **kwargs):
        Path(destination).write_text("new task", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(doctor.os, "link", target_appears)

    doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert lease.exists()
    assert target.read_text(encoding="utf-8") == "new task"


def test_concurrent_doctors_recover_a_dead_lease_once(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    lease = state_root / "run" / "queue" / "dead.processing"
    _write_lease(
        lease,
        pid=999999,
        acquired_at=(datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    def repair():
        return doctor.run_doctor(
            root=root, state_root=state_root, home=home, repair=True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: repair(), range(2)))

    recovered = sum(
        item.get("count", 0)
        for report in reports
        for item in report["repaired"]
        if item["action"] == "recover_stale_lease"
    )
    assert recovered == 1
    assert not lease.exists()
    assert lease.with_suffix(".json").exists()


def test_repair_never_mutates_personal_markdown(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    personal = root / "knowledge" / "notes" / "private.md"
    personal.write_text("private durable knowledge", encoding="utf-8")
    before = personal.read_bytes()
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state: None)

    doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert personal.read_bytes() == before


def test_failed_index_repair_preserves_existing_index(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    index.write_bytes(b"valid-index")
    old = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(index, (old.timestamp(), old.timestamp()))
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state: (_ for _ in ()).throw(RuntimeError("failed")))

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert index.read_bytes() == b"valid-index"
    assert report["overall_status"] in {"degraded", "error"}
    assert not any(item["action"] == "rebuild_index" for item in report["repaired"])


def test_failed_index_repair_is_attributed_only_to_index(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(
        doctor,
        "_rebuild_index",
        lambda root, state: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    runtime = _check(report, "runtime")
    queue = _check(report, "queue")
    index = _check(report, "index")

    assert index["status"] == "error"
    assert index["details"]["repair_errors"] == ["Index repair failed: RuntimeError"]
    assert "repair_errors" not in runtime["details"]
    assert "repair_errors" not in queue["details"]


def test_index_repair_that_produces_no_index_is_reported_as_index_error(
    tmp_path, monkeypatch
):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state: None)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    index = _check(report, "index")

    assert index["status"] == "error"
    assert index["details"]["repair_errors"] == [
        "Index repair failed: index was not created"
    ]


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
    assert "index" in summary
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


def test_cli_returns_zero_for_healthy_report(tmp_path):
    root, state_root, home = _build_root(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": today, "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    _create_index(state_root / "cache" / "index.sqlite")
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

    assert result.returncode == 0
    assert json.loads(result.stdout)["overall_status"] == "ok"


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


def test_cli_repair_json_is_idempotent(tmp_path):
    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "missing-state"
    env = os.environ.copy()
    env.update(
        LLM_WIKI_ROOT=str(root),
        LLM_WIKI_STATE_ROOT=str(state_root),
        HOME=str(home),
        USERPROFILE=str(home),
    )

    first = subprocess.run(
        [sys.executable, str(DOCTOR), "--repair", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    after_first = _snapshot(state_root)
    second = subprocess.run(
        [sys.executable, str(DOCTOR), "--repair", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    first_report = json.loads(first.stdout)
    second_report = json.loads(second.stdout)
    assert first.returncode == 0
    assert first_report["repaired"]
    assert second.returncode == 0
    assert second_report["repaired"] == []
    assert _snapshot(state_root) == after_first


def test_repair_does_not_touch_knowledge_config_network_or_subprocess(
    tmp_path, monkeypatch
):
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

    def local_rebuild(root_path, state_path):
        _create_index(state_path / "cache" / "index.sqlite")

    monkeypatch.setattr(doctor, "_rebuild_index", local_rebuild)
    monkeypatch.setattr(subprocess, "run", reject_external)
    monkeypatch.setattr(subprocess, "Popen", reject_external)
    monkeypatch.setattr(urllib.request, "urlopen", reject_external)
    monkeypatch.setattr(socket, "create_connection", reject_external)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert report["overall_status"] in {"ok", "degraded"}
    assert _snapshot(root) == before_root
    assert _snapshot(home) == before_home


def test_repair_rejects_symlinked_runtime_component(tmp_path):
    import shutil

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
    link = state_root / "run" / "queue" / "linked.json"
    _symlink_or_skip(link, external)

    report = run_doctor(root=root, state_root=state_root, home=home)
    queue = _check(report, "queue")

    assert queue["status"] == "degraded"
    assert queue["details"]["unsafe_entries"] == 1
    assert queue["details"]["pending"] == 0
    assert "OUTSIDE-SECRET" not in json.dumps(report)


def test_index_rejects_zero_byte_corrupt_and_wrong_schema(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    for content in (b"", b"not sqlite"):
        index.write_bytes(content)
        check = _check(run_doctor(root=root, state_root=state_root, home=home), "index")
        assert check["status"] == "error"
        assert check["details"]["freshness"] == "corrupt"
    index.unlink()
    connection = sqlite3.connect(index)
    connection.execute("CREATE TABLE other(value TEXT)")
    connection.close()
    check = _check(run_doctor(root=root, state_root=state_root, home=home), "index")
    assert check["status"] == "error"
    assert check["details"]["freshness"] == "corrupt"


def test_repair_never_replaces_corrupt_regular_index(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    corrupt = b"corrupt-index-bytes-that-must-be-preserved"
    index.write_bytes(corrupt)
    rebuild_calls = []
    monkeypatch.setattr(
        doctor,
        "_rebuild_index",
        lambda *args: rebuild_calls.append(args),
    )

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
    )
    check = _check(report, "index")

    assert check["status"] == "error"
    assert check["details"]["freshness"] == "corrupt"
    assert check["details"]["repairable"] is False
    assert rebuild_calls == []
    assert index.read_bytes() == corrupt
    assert not any(item["action"] == "rebuild_index" for item in report["repaired"])


def test_index_validates_manifest_against_indexed_paths(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index, ["knowledge/notes/one.md"], manifest=True)
    manifest = index.parent / ".paths-manifest"
    manifest.write_text(json.dumps(["knowledge/notes/two.md"]), encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "index")

    assert check["status"] == "error"
    assert check["details"]["freshness"] == "corrupt"


def test_index_symlink_is_rejected_and_external_target_untouched(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    external = tmp_path / "outside.sqlite"
    _create_index(external)
    link = state_root / "cache" / "index.sqlite"
    _symlink_or_skip(link, external)
    before = external.read_bytes()

    report = run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert _check(report, "index")["status"] == "error"
    assert external.read_bytes() == before


def test_rebuild_collects_only_safe_regular_markdown(tmp_path, monkeypatch):
    import doctor
    import search_memory

    root, state_root, _ = _build_root(tmp_path)
    notes = root / "knowledge" / "notes"
    safe = notes / "safe.md"
    safe.write_text("# Safe", encoding="utf-8")
    external = tmp_path / "outside.md"
    external.write_text("# Secret", encoding="utf-8")
    _symlink_or_skip(notes / "linked.md", external)
    captured = []
    monkeypatch.setattr(search_memory, "_build_index", lambda pages: captured.extend(pages))

    doctor._rebuild_index(root, state_root)

    assert captured == [safe]


def test_rebuild_rejects_symlinked_knowledge_ancestor(tmp_path, monkeypatch):
    import shutil

    import doctor
    import search_memory

    root, state_root, _ = _build_root(tmp_path)
    external = tmp_path / "outside-knowledge"
    (external / "notes").mkdir(parents=True)
    (external / "notes" / "secret.md").write_text("# Secret", encoding="utf-8")
    shutil.rmtree(root / "knowledge")
    _symlink_or_skip(root / "knowledge", external, directory=True)
    captured = []
    monkeypatch.setattr(search_memory, "_build_index", lambda pages: captured.extend(pages))

    with pytest.raises(OSError, match="unsafe knowledge"):
        doctor._rebuild_index(root, state_root)

    assert captured == []


def test_rebuild_that_does_not_change_stale_index_is_not_success(
    tmp_path, monkeypatch
):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(index, (old.timestamp(), old.timestamp()))
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state: None)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert not any(item["action"] == "rebuild_index" for item in report["repaired"])
    assert _check(report, "index")["status"] == "error"


def test_existing_index_rebuild_lock_defers_without_touching_live_index(
    tmp_path, monkeypatch
):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(index, (old.timestamp(), old.timestamp()))
    before = index.read_bytes()
    _write_lock(
        state_root / "cache" / ".doctor-index.lock",
        pid=os.getpid(),
        acquired_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    called = []
    monkeypatch.setattr(doctor, "_rebuild_index", lambda *args: called.append(True))

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert called == []
    assert index.read_bytes() == before
    check = _check(report, "index")
    assert check["status"] == "degraded"
    assert check["details"]["repair_deferred"] is True
    assert "deferred" in check["message"].lower()


def test_dead_index_rebuild_lock_is_reclaimed(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    lock = state_root / "cache" / ".doctor-index.lock"
    _write_lock(
        lock,
        pid=999999,
        acquired_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        doctor,
        "_rebuild_index",
        lambda root, state: _create_index(state / "cache" / "index.sqlite"),
    )

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert not lock.exists()
    assert any(item["action"] == "rebuild_index" for item in report["repaired"])
    assert _check(report, "index")["status"] == "ok"


def test_concurrent_stale_lock_reclaimers_cannot_both_acquire(
    tmp_path, monkeypatch
):
    import threading

    import doctor

    queue = tmp_path / "queue"
    queue.mkdir()
    lock = queue / ".doctor-recovery.lock"
    now = datetime.now(timezone.utc)
    _write_lock(
        lock,
        pid=999999,
        acquired_at=(now - timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)
    real_os_lock = doctor._lock_file_nonblocking
    first_locked = threading.Event()
    release_first = threading.Event()
    control_lock = threading.Lock()
    first = True

    def controlled_os_lock(fd):
        nonlocal first
        acquired = real_os_lock(fd)
        with control_lock:
            pause = acquired and first
            if pause:
                first = False
        if pause:
            first_locked.set()
            assert release_first.wait(timeout=2)
        return acquired

    monkeypatch.setattr(doctor, "_lock_file_nonblocking", controlled_os_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(doctor._acquire_lock, lock, queue, now)
        assert first_locked.wait(timeout=2)
        second_future = pool.submit(doctor._acquire_lock, lock, queue, now)
        second_token = second_future.result(timeout=2)
        release_first.set()
        first_token = first_future.result(timeout=2)

    assert sum(token is not None for token in (first_token, second_token)) == 1
    token = first_token or second_token
    doctor._release_lock(lock, queue, token)


def test_stale_lock_takeover_aborts_when_opened_file_identity_changes(
    tmp_path, monkeypatch
):
    import doctor

    queue = tmp_path / "queue"
    queue.mkdir()
    lock = queue / ".doctor-recovery.lock"
    now = datetime.now(timezone.utc)
    _write_lock(
        lock,
        pid=999999,
        acquired_at=(now - timedelta(hours=1)).isoformat(),
    )
    replacement = queue / "replacement.lock"
    _write_lock(
        replacement,
        pid=888888,
        acquired_at=(now - timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)
    real_stat = doctor.os.stat

    def replaced_path_identity(path, *args, **kwargs):
        if Path(path) == lock and kwargs.get("follow_symlinks") is False:
            return replacement.lstat()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(doctor.os, "stat", replaced_path_identity)

    token = doctor._acquire_lock(lock, queue, now)

    assert token is None
    assert lock.exists()


@pytest.mark.parametrize("active", [True, False])
def test_queue_recovery_lock_is_owner_aware(tmp_path, monkeypatch, active):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")
    queue = state_root / "run" / "queue"
    lease = queue / "dead.processing"
    _write_lease(
        lease,
        pid=999999,
        acquired_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    lock = queue / ".doctor-recovery.lock"
    _write_lock(
        lock,
        pid=os.getpid() if active else 888888,
        acquired_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setattr(
        doctor,
        "_pid_alive",
        lambda pid: active if pid in {os.getpid(), 888888} else False,
    )

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    check = _check(report, "queue")

    if active:
        assert lease.exists()
        assert check["details"]["repair_deferred"] is True
        assert "deferred" in check["message"].lower()
    else:
        assert not lease.exists()
        assert lease.with_suffix(".json").exists()
        assert not lock.exists()


def test_queue_scan_is_bounded_by_count_and_file_size(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    queue = state_root / "run" / "queue"
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

    assert elapsed < 0.5
    assert details["truncated"] is True
    assert details["scanned"] <= 200
    assert _check(report, "queue")["status"] == "degraded"


def test_oversized_state_is_bounded_and_reported(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    (state_root / "run" / "state.json").write_bytes(b"x" * 300_000)

    report = run_doctor(root=root, state_root=state_root, home=home)
    scheduler = _check(report, "scheduler")

    assert scheduler["status"] == "degraded"
    assert scheduler["details"]["state_error"] == "oversized"


def test_index_check_honors_expired_deadline(tmp_path):
    import doctor

    _, state_root, _ = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")

    started = time.perf_counter()
    check = doctor._index_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=time.monotonic() - 1,
    )

    assert time.perf_counter() - started < 0.2
    assert check["status"] == "degraded"
    assert check["details"]["budget_exhausted"] is True


def test_budget_exhaustion_degrades_overall_and_health_summary(tmp_path):
    from doctor import degraded_summary, run_doctor

    root, state_root, home = _build_root(tmp_path)
    _create_index(state_root / "cache" / "index.sqlite")

    report = run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=0,
    )
    index = _check(report, "index")

    assert index["status"] == "degraded"
    assert index["details"]["budget_exhausted"] is True
    assert report["overall_status"] != "ok"
    assert "index" in degraded_summary(report)


def test_locked_index_returns_immediately_as_degraded(tmp_path):
    import doctor

    _, state_root, _ = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    writer = sqlite3.connect(index, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        check = doctor._index_check(
            state_root,
            datetime.now(timezone.utc),
            deadline=time.monotonic() + 0.2,
        )
        elapsed = time.perf_counter() - started
    finally:
        writer.rollback()
        writer.close()

    assert elapsed < 0.5
    assert check["status"] == "degraded"
    assert check["details"]["database_busy"] is True


def test_indexed_path_scan_detects_bounded_overflow(tmp_path, monkeypatch):
    import doctor

    _, state_root, _ = _build_root(tmp_path)
    monkeypatch.setattr(doctor, "MAX_INDEX_PATHS", 10)
    _create_index(
        state_root / "cache" / "index.sqlite",
        [f"knowledge/notes/{number}.md" for number in range(11)],
    )

    check = doctor._index_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
    )

    assert check["status"] == "degraded"
    assert check["details"]["path_limit_exceeded"] is True


def test_fts_shadow_corruption_is_not_reported_ok(tmp_path):
    import doctor

    _, state_root, _ = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index, ["knowledge/notes/one.md"])
    connection = sqlite3.connect(index)
    connection.execute("DROP TABLE pages_idx")
    connection.commit()
    connection.close()

    check = doctor._index_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
    )

    assert check["status"] == "error"
    assert check["details"]["freshness"] == "corrupt"


def test_unrelated_installed_configs_are_not_false_positives(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    configs = {
        home / ".claude" / "settings.json": "unrelated claude config",
        home / ".config" / "opencode" / "plugins" / "llm-wiki-memory.js": "unrelated plugin",
        home / ".codex" / "config.toml": "unrelated codex config",
        home / ".cursor" / "rules" / "llm-wiki.mdc": "unrelated cursor rule",
        home / ".gemini" / "antigravity" / "AGENTS.md": "unrelated agent rules",
    }
    for path, content in configs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "degraded"
    assert all(item["status"] == "degraded" for item in check["details"]["hosts"].values())


@pytest.mark.parametrize(
    ("host", "relative", "marker"),
    [
        ("claude", ".claude/settings.json", "LLM_WIKI_ROOT session_start_context.py"),
        (
            "opencode",
            ".config/opencode/plugins/llm-wiki-memory.js",
            "session.created LLM_WIKI_ROOT",
        ),
        ("codex", ".codex/config.toml", "codex-memory-wrapper codex_memory"),
        ("cursor", ".cursor/rules/llm-wiki.mdc", "LLM-Wiki LLM_WIKI_ROOT"),
        (
            "antigravity",
            ".gemini/antigravity/AGENTS.md",
            "LLM-Wiki LLM_WIKI_ROOT",
        ),
    ],
)
def test_installed_integration_requires_expected_marker(
    tmp_path, host, relative, marker
):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"][host]["status"] == "ok"
