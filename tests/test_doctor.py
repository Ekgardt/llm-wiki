from __future__ import annotations

import io
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


def _codex_hooks_fixture() -> dict:
    command = {
        "type": "command",
        "command": 'uv run --directory "$LLM_WIKI_ROOT" python "$LLM_WIKI_ROOT/scripts/codex_memory.py" hook',
        "commandWindows": 'uv run --directory "%LLM_WIKI_ROOT%" python "%LLM_WIKI_ROOT%\\scripts\\codex_memory.py" hook',
        "timeout": 15,
    }
    return {
        "hooks": {
            "SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [command]}],
            "PreCompact": [{"matcher": "manual|auto", "hooks": [command]}],
            "PostCompact": [{"matcher": "manual|auto", "hooks": [command]}],
            "Stop": [{"hooks": [command]}],
        }
    }


def _runtime_hooks(root: Path, *, trust: str = "trusted", enabled: bool = True) -> dict:
    template = _codex_hooks_fixture()["hooks"]
    hooks = []
    for event_name, groups in template.items():
        group = groups[0]
        handler = group["hooks"][0]
        hooks.append(
            {
                "eventName": event_name,
                "matcher": group.get("matcher"),
                "command": handler["commandWindows"] if os.name == "nt" else handler["command"],
                "enabled": enabled,
                "trustStatus": trust,
                "source": "user",
                "currentHash": "never-report-this-hash",
            }
        )
    return {"data": [{"cwd": str(root), "hooks": hooks, "warnings": [], "errors": []}]}


def _create_index(path: Path, paths: list[str] | None = None, manifest: bool = True) -> None:
    paths = paths or []
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE pages USING fts5("
        "path UNINDEXED, title, summary, body, project UNINDEXED, "
        "timestamp UNINDEXED, slug, tokenize = 'porter unicode61')"
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


def _create_claim_index(root: Path, state_root: Path) -> None:
    from claims import ClaimIndex

    ClaimIndex(state_root, vault=root).rebuild([root / "knowledge" / "notes"])


def _create_generation(state_root: Path) -> None:
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog

    build_full_generation(
        GenerationCatalog(state_root),
        sources=(),
        source_bytes={},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="healthy-generation",
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
        root / "integrations" / "codex",
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
    (root / "integrations" / "codex" / "hooks.json").write_text(
        json.dumps(_codex_hooks_fixture(), indent=2) + "\n", encoding="utf-8"
    )
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
    _create_claim_index(root, state_root)
    _create_generation(state_root)
    os.utime(index, (now.timestamp(), now.timestamp()))

    report = run_doctor(root=root, state_root=state_root, home=home, now=now)

    assert report["schema_version"] == "1.0"
    assert report["generated_at"].endswith("+00:00")
    assert report["overall_status"] == "ok"
    assert {item["id"] for item in report["checks"]} == {
        "environment",
        "runtime",
        "filesystem",
        "transactions",
        "queue",
        "archives",
        "claims",
        "generation",
        "index",
        "scheduler",
        "mcp",
        "integrations",
        "run_deletion",
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
    import doctor
    import reliable_memory

    monkeypatch.setattr(reliable_memory, "_platform_system", lambda: system)
    monkeypatch.setattr(reliable_memory, "_read_posix_mount_data", lambda: (mount_data, False))
    monkeypatch.setattr(reliable_memory, "_query_darwin_mounts", lambda: darwin_data)
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
        "_sqlite_lock_probe",
        lambda *args, **kwargs: pytest.fail("lock probe ran on network storage"),
    )

    check = doctor._filesystem_check(Path(state_path))

    assert check["status"] == "error"
    assert check["details"] == {"local": False, "locking": "unsupported"}


def test_filesystem_health_distinguishes_broken_and_unavailable_lock_probe(tmp_path, monkeypatch):
    import doctor
    import reliable_memory

    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setattr(reliable_memory, "_known_network_path", lambda path: False)
    monkeypatch.setattr(reliable_memory, "_sqlite_lock_probe", lambda path, **kwargs: False)
    broken = doctor._filesystem_check(state_root)
    monkeypatch.setattr(reliable_memory, "_sqlite_lock_probe", lambda path, **kwargs: None)
    unavailable = doctor._filesystem_check(state_root)

    assert broken["status"] == "error"
    assert broken["details"] == {"local": True, "locking": "unsupported"}
    assert unavailable["status"] == "degraded"
    assert unavailable["details"] == {"local": True, "locking": "unknown"}


def test_filesystem_health_runs_bounded_probe_and_leaves_no_artifacts(tmp_path, monkeypatch):
    import doctor
    import reliable_memory

    state_root = tmp_path / "state"
    state_root.mkdir()
    before = _snapshot(tmp_path)
    calls = []
    real_probe = reliable_memory._sqlite_lock_probe

    def probe(path, *, deadline):
        calls.append((path, deadline))
        return real_probe(path, deadline=deadline)

    monkeypatch.setattr(reliable_memory, "_known_network_path", lambda path: False)
    monkeypatch.setattr(reliable_memory, "_sqlite_lock_probe", probe)

    check = doctor._filesystem_check(state_root, deadline=time.monotonic() + 1)

    assert check["status"] == "ok"
    assert calls and calls[0][0] == state_root
    assert calls[0][1] != float("inf")
    assert _snapshot(tmp_path) == before
    assert not list(state_root.glob(".llm-wiki-lock-probe-*"))


def test_read_only_missing_runtime_does_not_create_it(tmp_path):
    from doctor import run_doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "absent-state"

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "runtime")
    environment = _check(run_doctor(root=root, state_root=state_root, home=home), "environment")

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
    assert (
        _check(run_doctor(root=root, state_root=state_root, home=home, now=now), "index")["status"]
        == "degraded"
    )

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


def test_codex_mcp_config_alone_does_not_claim_capture(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["python", "scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    codex = check["details"]["hosts"]["codex"]
    assert codex["status"] == "degraded"
    assert codex["capture_mode"] == "none"


def test_codex_quoted_mcp_table_is_accepted(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers."llm-wiki"]\ncommand = "uv"\n'
        'args = ["run", "python", "scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "degraded"


def test_codex_doctor_prefers_trusted_runtime_hooks(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    (codex_dir / "hooks.json").write_bytes(
        (root / "integrations" / "codex" / "hooks.json").read_bytes()
    )

    monkeypatch.setattr(
        doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root)
    )
    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "ok"
    assert codex["capture_mode"] == "official-hooks"
    assert codex["trust"] == "review-with-/hooks"
    assert "currentHash" not in json.dumps(codex)


def test_codex_runtime_hook_health_is_decoupled_from_mcp_config(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    monkeypatch.setattr(
        doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root)
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "ok"


@pytest.mark.parametrize("trust", ["untrusted", "modified"])
def test_codex_runtime_untrusted_or_modified_is_degraded_without_capture(
    tmp_path, monkeypatch, trust
):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    monkeypatch.setattr(
        doctor,
        "_probe_codex_hooks_list",
        lambda *_args, **_kwargs: _runtime_hooks(root, trust=trust),
    )

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["capture_mode"] == "none"
    assert codex["reason"] == f"runtime_hooks_{trust}"


def test_codex_runtime_probe_warnings_are_degraded(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    response = _runtime_hooks(root)
    response["data"][0]["warnings"] = ["configuration warning"]
    monkeypatch.setattr(doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: response)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_warning_or_error"
    assert "configuration warning" not in json.dumps(codex)


def test_codex_unavailable_probe_reports_unverified_and_no_capture(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    monkeypatch.setattr(doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: None)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_unverified"
    assert codex["capture_mode"] == "none"


def test_codex_configured_wrapper_is_reported_as_heartbeat_fallback(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (root / "scripts" / "codex-memory-wrapper.ps1").write_text(
        "function codex {}\n", encoding="utf-8"
    )
    (home / ".codex").mkdir()
    profile = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '. "$env:LLM_WIKI_ROOT\\scripts\\codex-memory-wrapper.ps1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: None)

    codex = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")[
        "details"
    ]["hosts"]["codex"]

    assert codex["capture_mode"] == "wrapper-fallback-heartbeat-only"


def test_codex_hooks_probe_skips_spawn_when_deadline_budget_is_too_small(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    (home / ".codex").mkdir()
    monkeypatch.setattr(
        doctor,
        "_codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )
    monkeypatch.setattr(
        doctor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("insufficient budget spawned Codex"),
    )

    deadline = time.monotonic() + doctor.CODEX_HOOK_PROBE_STARTUP_SECONDS / 2
    response = doctor._probe_codex_hooks_list(root, home, deadline=deadline)
    check = doctor._integration_check(root, home, deadline=deadline)

    assert response is None
    codex = check["details"]["hosts"]["codex"]
    assert codex["reason"] == "runtime_hooks_not_completed"
    assert codex["not_completed"] is True


def test_codex_hooks_probe_cleanup_honors_absolute_deadline(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    clock = [100.0]
    waits = []

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            waits.append(timeout)
            clock[0] += timeout
            raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            return None

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor.subprocess, "Popen", Process)
    monkeypatch.setattr(
        doctor,
        "_codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    active, reason = doctor._codex_runtime_hooks_state(root, home, deadline=100.5)

    assert active is False
    assert reason == "runtime_hooks_not_completed"
    assert waits[0] == pytest.approx(0.5)
    assert sum(waits) <= 0.500001


def test_codex_hooks_probe_own_timeout_remains_unverified(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    clock = [100.0]

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            clock[0] += timeout
            raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            return None

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor.subprocess, "Popen", Process)
    monkeypatch.setattr(
        doctor,
        "_codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    active, reason = doctor._codex_runtime_hooks_state(root, home, deadline=105.0)

    assert active is False
    assert reason == "runtime_hooks_unverified"


def test_codex_hooks_probe_is_bounded_and_uses_exact_cwd(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    observed = {}

    class Input(io.BytesIO):
        def close(self):
            pass

    class Process:
        def __init__(self, args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            self.returncode = 0
            response = {
                "id": 2,
                "result": _runtime_hooks(root),
            }
            self.stdin = Input()
            self.stdout = io.BytesIO((json.dumps(response) + "\n").encode())
            self.stderr = io.BytesIO()

        def wait(self, timeout=None):
            observed["timeout"] = timeout
            observed["input"] = self.stdin.getvalue().decode()
            return self.returncode

        def kill(self):
            pytest.fail("bounded probe unexpectedly timed out")

    monkeypatch.setattr(doctor.subprocess, "Popen", Process)
    monkeypatch.setattr(
        doctor,
        "_codex_app_server_command",
        lambda: ["codex", "app-server", "--listen", "stdio://"],
    )

    response = doctor._probe_codex_hooks_list(
        root,
        home,
        deadline=time.monotonic() + doctor.CODEX_HOOK_PROBE_SECONDS,
    )

    requests = [json.loads(line) for line in observed["input"].splitlines()]
    assert observed["args"] == ["codex", "app-server", "--listen", "stdio://"]
    assert observed["kwargs"]["cwd"] == str(root)
    assert observed["kwargs"]["env"]["CODEX_HOME"] == str(home / ".codex")
    assert observed["kwargs"]["stdout"] is subprocess.PIPE
    assert observed["kwargs"]["stderr"] is subprocess.PIPE
    assert observed["timeout"] <= doctor.CODEX_HOOK_PROBE_SECONDS
    assert requests[0]["method"] == "initialize"
    assert requests[1]["method"] == "initialized"
    assert requests[2] == {"id": 2, "method": "hooks/list", "params": {"cwds": [str(root)]}}
    assert "bypass" not in observed["input"].casefold()
    assert response == _runtime_hooks(root)


def test_codex_app_server_command_supports_windows_cmd_shim(monkeypatch):
    import doctor

    shim = r"C:\Users\Test User\AppData\Roaming\npm\codex.cmd"
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: shim if name == "codex.cmd" else None,
    )
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")

    command = doctor._codex_app_server_command(platform="nt")

    assert command[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]
    assert "codex.cmd" in command[4]
    assert "app-server" in command[4]


def test_codex_app_server_command_uses_direct_executable_on_unix(monkeypatch):
    import doctor

    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )

    assert doctor._codex_app_server_command(platform="posix") == [
        "/usr/local/bin/codex",
        "app-server",
        "--listen",
        "stdio://",
    ]


def test_codex_doctor_rejects_runtime_disabled_hooks(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(
        "[features]\nhooks = false\n\n[mcp_servers.llm-wiki]\n"
        'command = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    hooks = json.loads((root / "integrations" / "codex" / "hooks.json").read_text(encoding="utf-8"))
    hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 999
    (codex_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    monkeypatch.setattr(
        doctor,
        "_probe_codex_hooks_list",
        lambda *_args, **_kwargs: _runtime_hooks(root, enabled=False),
    )
    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "degraded"
    assert codex["reason"] == "runtime_hooks_disabled"
    assert codex["capture_mode"] == "none"


@pytest.mark.parametrize(
    "content",
    [
        '# [mcp_servers.llm-wiki]\n# command = "uv"\n# args = ["scripts/mcp_server.py"]\n',
        'description = "mcp_servers.llm-wiki command uv scripts/mcp_server.py"\n',
        '[other]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\nenabled = false\n',
        '[mcp_servers."llm-wiki"\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
    ],
    ids=["commented", "unrelated-string", "unrelated-table", "disabled", "malformed"],
)
def test_codex_config_requires_active_valid_toml_table(tmp_path, content):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(content, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"]["codex"]["status"] == "degraded"


def test_codex_parser_prefers_stdlib_tomllib(monkeypatch):
    import doctor

    calls = []

    class Parser:
        @staticmethod
        def loads(text):
            calls.append(text)
            return {"source": "stdlib"}

    class Backport:
        @staticmethod
        def loads(text):
            pytest.fail("Tomli used while stdlib tomllib was available")

    monkeypatch.setattr(doctor, "STDLIB_TOML", Parser, raising=False)
    monkeypatch.setattr(doctor, "TOMLI", Backport, raising=False)

    document, error = doctor._parse_toml_document("key = 'value'")

    assert document == {"source": "stdlib"}
    assert error is None
    assert calls == ["key = 'value'"]


def test_codex_parser_uses_tomli_when_stdlib_unavailable(monkeypatch):
    import doctor

    calls = []

    class Parser:
        @staticmethod
        def loads(text):
            calls.append(text)
            return {"source": "tomli"}

    monkeypatch.setattr(doctor, "STDLIB_TOML", None, raising=False)
    monkeypatch.setattr(doctor, "TOMLI", Parser, raising=False)

    document, error = doctor._parse_toml_document("key = 'value'")

    assert document == {"source": "tomli"}
    assert error is None
    assert calls == ["key = 'value'"]


def test_codex_hook_health_does_not_use_local_toml_parser(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "STDLIB_TOML", None, raising=False)
    monkeypatch.setattr(doctor, "TOMLI", None, raising=False)
    monkeypatch.setattr(
        doctor, "_probe_codex_hooks_list", lambda *_args, **_kwargs: _runtime_hooks(root)
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "integrations")
    codex = check["details"]["hosts"]["codex"]

    assert codex["status"] == "ok"


def test_codex_real_parser_rejects_malformed_surrounding_toml(tmp_path):
    import doctor

    config = tmp_path / "config.toml"
    config.write_text(
        'broken = [\n[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]\n',
        encoding="utf-8",
    )

    configured, reason = doctor._codex_config_state(config)

    assert configured is False
    assert reason == "toml_invalid"


def test_codex_parser_input_remains_file_bounded(tmp_path, monkeypatch):
    import doctor

    config = tmp_path / "config.toml"
    config.write_text(
        '[mcp_servers.llm-wiki]\ncommand = "uv"\n'
        'args = ["scripts/mcp_server.py"]\n# padding padding padding\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "MAX_CONFIG_BYTES", 32)

    configured, reason = doctor._codex_config_state(config)

    assert configured is False
    assert reason == "config_missing_or_unsafe"


def test_project_scoped_cursor_and_antigravity_are_advisory(tmp_path):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    (home / ".cursor").mkdir()
    (home / ".gemini" / "antigravity").mkdir(parents=True)

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "ok"
    assert check["details"]["hosts"]["cursor"]["status"] == "skipped"
    assert check["details"]["hosts"]["antigravity"]["status"] == "skipped"


def test_repair_creates_runtime_and_is_idempotent(tmp_path, monkeypatch):
    import doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "new-state"
    rebuilt = []

    def rebuild(root, state, **kwargs):
        rebuilt.append(state)
        _create_index(state / "cache" / "index.sqlite")

    monkeypatch.setattr(doctor, "_rebuild_index", rebuild)

    first = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    after_first = _snapshot(state_root)
    second = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert {str(state_root / item) for item in ("run", "logs", "cache")} <= {
        str(path) for path in state_root.rglob("*") if path.is_dir()
    }
    assert any(item["action"] == "create_runtime_directory" for item in first["repaired"])
    assert second["repaired"] == []
    assert set(_snapshot(state_root)) == set(after_first)
    assert len(rebuilt) == 1


def test_maintenance_owner_is_exclusive_heartbeated_released_and_fenced(tmp_path):
    import doctor

    root, state_root, _ = _build_root(tmp_path)
    now = datetime.now(timezone.utc)

    first = doctor._acquire_maintenance_owner(root, state_root, now)
    assert first is not None
    coordinator, lease = first
    assert doctor._acquire_maintenance_owner(root, state_root, now) is None

    doctor._heartbeat_maintenance_owner(coordinator, lease, now + timedelta(seconds=1))
    doctor._release_maintenance_owner(coordinator, lease)
    second = doctor._acquire_maintenance_owner(root, state_root, now + timedelta(seconds=2))

    assert second is not None
    second_coordinator, second_lease = second
    assert second_lease["epoch"] == lease["epoch"] + 1
    doctor._release_maintenance_owner(second_coordinator, second_lease)


def test_live_pid_prevents_expired_maintenance_owner_reclaim(tmp_path, monkeypatch):
    import doctor

    root, state_root, _ = _build_root(tmp_path)
    now = datetime.now(timezone.utc)
    first = doctor._acquire_maintenance_owner(root, state_root, now)
    assert first is not None
    coordinator, lease = first
    with coordinator._connect() as database:
        database.execute(
            "UPDATE maintenance_owners SET expires_at=? WHERE owner_name='doctor'",
            ((now - timedelta(minutes=1)).isoformat(),),
        )
        database.commit()
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: pid == os.getpid())

    assert doctor._acquire_maintenance_owner(root, state_root, now) is None
    doctor._release_maintenance_owner(coordinator, lease)


def test_maintenance_heartbeat_runs_during_long_operation(tmp_path, monkeypatch):
    import doctor

    root, state_root, _ = _build_root(tmp_path)
    acquired = doctor._acquire_maintenance_owner(root, state_root, datetime.now(timezone.utc))
    assert acquired is not None
    coordinator, lease = acquired
    beats = []
    real = doctor._heartbeat_maintenance_owner

    def heartbeat(*args, **kwargs):
        beats.append(time.monotonic())
        return real(*args, **kwargs)

    monkeypatch.setattr(doctor, "MAINTENANCE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(doctor, "_heartbeat_maintenance_owner", heartbeat)

    with doctor._MaintenanceHeartbeat(coordinator, lease, deadline=time.monotonic() + 1) as guard:
        guard.run(lambda: time.sleep(0.05))

    assert len(beats) >= 2


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
    assert not (state_root / "run" / "queue-migrated-v2").exists()
    assert _check(report, "queue")["details"]["migration"] == "pending"


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
    assert any((state_root / "run" / "queue-quarantine").iterdir())
    assert any(item["action"] == "recover_stale_lease" for item in report["repaired"])


def test_lease_recovery_never_clobbers_target_appearing_concurrently(tmp_path, monkeypatch):
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

    assert not lease.exists()
    assert not target.exists()
    assert any((state_root / "run" / "queue-quarantine").iterdir())


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
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: pid == os.getpid())

    def repair():
        return doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

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
    assert (state_root / "run" / "queue-migrated-v2").exists()


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
    monkeypatch.setattr(
        doctor, "_rebuild_index", lambda root, state: (_ for _ in ()).throw(RuntimeError("failed"))
    )

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
        lambda root, state, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    runtime = _check(report, "runtime")
    queue = _check(report, "queue")
    index = _check(report, "index")

    assert index["status"] == "error"
    assert index["details"]["repair_errors"] == ["Index repair failed: RuntimeError"]
    assert "repair_errors" not in runtime["details"]
    assert "repair_errors" not in queue["details"]


def test_index_repair_that_produces_no_index_is_reported_as_index_error(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor, "_rebuild_index", lambda root, state, **kwargs: None)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)
    index = _check(report, "index")

    assert index["status"] == "error"
    assert index["details"]["repair_errors"] == ["Index repair failed: index was not created"]


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
    _create_claim_index(root, state_root)
    _create_generation(state_root)
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
    assert first.returncode == 1
    assert first_report["repaired"]
    assert first_report["overall_status"] == "degraded"
    assert second.returncode == 1
    assert second_report["overall_status"] == "degraded"
    assert second_report["repaired"] == []
    assert set(_snapshot(state_root)) == set(after_first)


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

    def local_rebuild(root_path, state_path, **kwargs):
        _create_index(
            state_path / "cache" / "index.sqlite",
            ["knowledge/notes/private.md"],
        )

    monkeypatch.setattr(doctor, "_rebuild_index", local_rebuild)
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

    assert check["status"] == "degraded"
    assert check["details"]["freshness"] == "stale"
    assert check["details"]["source_rebuild_required"] is True


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


def test_rebuild_that_does_not_change_stale_index_is_not_success(tmp_path, monkeypatch):
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


def test_existing_index_rebuild_lock_defers_without_touching_live_index(tmp_path, monkeypatch):
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
        lambda root, state, **kwargs: _create_index(state / "cache" / "index.sqlite"),
    )

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, repair=True)

    assert not lock.exists()
    assert any(item["action"] == "rebuild_index" for item in report["repaired"])
    assert _check(report, "index")["status"] == "ok"


def test_concurrent_stale_lock_reclaimers_cannot_both_acquire(tmp_path, monkeypatch):
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


def test_stale_lock_takeover_aborts_when_opened_file_identity_changes(tmp_path, monkeypatch):
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
        assert (state_root / "run" / "queue-migrated-v2").exists()
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
    assert check["details"]["hosts"]["claude"]["status"] == "degraded"
    assert check["details"]["hosts"]["opencode"]["status"] == "degraded"
    assert check["details"]["hosts"]["codex"]["status"] == "degraded"
    assert check["details"]["hosts"]["cursor"]["status"] == "skipped"
    assert check["details"]["hosts"]["antigravity"]["status"] == "skipped"


@pytest.mark.parametrize(
    ("host", "relative", "marker"),
    [
        ("claude", ".claude/settings.json", "LLM_WIKI_ROOT session_start_context.py"),
        (
            "opencode",
            ".config/opencode/plugins/llm-wiki-memory.js",
            "session.created LLM_WIKI_ROOT",
        ),
        (
            "codex",
            ".codex/config.toml",
            '[mcp_servers.llm-wiki]\ncommand = "uv"\nargs = ["scripts/mcp_server.py"]',
        ),
        ("cursor", ".cursor/rules/llm-wiki.mdc", "LLM-Wiki LLM_WIKI_ROOT"),
        (
            "antigravity",
            ".gemini/antigravity/AGENTS.md",
            "LLM-Wiki LLM_WIKI_ROOT",
        ),
    ],
)
def test_installed_integration_requires_expected_marker(tmp_path, host, relative, marker):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    expected = "degraded" if host == "codex" else "ok"
    assert check["details"]["hosts"][host]["status"] == expected
