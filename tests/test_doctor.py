from __future__ import annotations

import errno
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
DOCTOR = SCRIPTS / "doctor.py"


GENEROUS_BUDGET_SECONDS = 120.0


@pytest.fixture(autouse=True)
def _budget_that_survives_a_slow_runner(monkeypatch):
    """Give every doctor run in this file enough time to reach its findings.

    `run_doctor` bounds itself at five seconds and reports "budget exhausted"
    instead of a finding when it runs out. On a loaded hosted Windows runner a
    single repair pass took 8.5 seconds, so tests asserting on findings were
    reading the clock rather than the behaviour. Tests about the budget itself
    pass their own value and keep it.
    """
    import doctor

    original = doctor.run_doctor

    def run(**kwargs):
        kwargs.setdefault("time_budget_seconds", GENEROUS_BUDGET_SECONDS)
        return original(**kwargs)

    monkeypatch.setattr(doctor, "run_doctor", run)


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


def _create_claim_index(root: Path, state_root: Path) -> None:
    from claims import ClaimIndex

    ClaimIndex(state_root, vault=root).rebuild([root / "knowledge" / "notes"])


def _create_generation(root: Path, state_root: Path) -> None:
    import corpus_snapshot
    import doctor
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    snapshot = corpus_snapshot.collect_corpus(root)
    build_full_generation(
        GenerationCatalog(state_root),
        sources=(
            {
                "source_id": source.record.logical_id,
                "relative_path": source.record.relative_path,
                "sha256": source.record.sha256,
                "size": source.record.size,
                "media_type": source.record.media_type,
                "language": source.record.language,
                "git_oid": source.record.git_oid,
            }
            for source in snapshot.sources
        ),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id="healthy-generation",
        graph_extractor_version=doctor._maintenance_extractor_identity(),
        repository_scope=resolve_repository_scope(root),
        snapshot=snapshot,
        publication_root=root,
    )


def test_doctor_accepts_active_graph_v2_without_code_capture(tmp_path: Path) -> None:
    import doctor
    from generation_catalog import GenerationCatalog

    root, state_root, home = _build_root(tmp_path)
    _create_generation(root, state_root)
    manifest = GenerationCatalog(state_root)._registered_manifest("healthy-generation")
    report = doctor.run_doctor(root=root, state_root=state_root, home=home)

    assert "code_capture" not in manifest
    assert _check(report, "generation")["status"] == "ok"


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


def _junction_or_skip(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions are unavailable on this platform")
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junctions unavailable: {result.stderr!r}")


def _broken_directory_link_or_skip(link: Path, target: Path) -> None:
    if os.name == "nt":
        target.mkdir()
        _junction_or_skip(link, target)
        try:
            target.rmdir()
        except OSError as exc:
            pytest.skip(f"could not make a dangling Windows junction: {exc}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


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
    source_root = Path(__file__).resolve().parent.parent
    for relative in (
        "integrations/cursor/hooks.json",
        "integrations/antigravity/hooks.json",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / relative).read_bytes())
    (root / "integrations" / "codex" / "hooks.json").write_text(
        json.dumps(_codex_hooks_fixture(), indent=2) + "\n", encoding="utf-8"
    )
    return root, state_root, home


def _check(report: dict, check_id: str) -> dict:
    return next(item for item in report["checks"] if item["id"] == check_id)


def _qualified_pyright_check(*_args, **_kwargs) -> dict:
    return {
        "id": "pyright",
        "status": "ok",
        "message": "Pyright identity is qualified.",
        "details": {
            "status": "qualified",
            "qualified": True,
            "codes": [],
        },
    }


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_report_schema_and_all_check_classes_are_json_safe(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": "2026-07-13", "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    _create_claim_index(root, state_root)
    _create_generation(root, state_root)
    os.utime(index, (now.timestamp(), now.timestamp()))

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, now=now)

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
        "capture",
        "mcp",
        "integrations",
        "pyright",
        "lsp",
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
        return {"overall_status": "ok", "repaired": [], "checks": []}

    monkeypatch.setattr(doctor, "run_doctor", record)

    assert doctor.main(["--time-budget", "45"]) == 0
    assert seen["time_budget_seconds"] == 45.0
    assert doctor.main([]) == 0
    assert seen["time_budget_seconds"] == doctor.DEFAULT_TIME_BUDGET_SECONDS

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


def test_read_only_run_never_attempts_a_write(tmp_path, monkeypatch):
    import doctor

    root, state_root, home = _build_root(tmp_path)

    real_open = doctor.os.open

    def reject_write(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags:
            raise AssertionError("read-only doctor attempted a write")
        return real_open(path, flags, *args, **kwargs)

    def reject_path_write(*args, **kwargs):
        raise AssertionError("read-only doctor attempted a write")

    monkeypatch.setattr(doctor.os, "open", reject_write)
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
        "LLM_WIKI_ROOT integration_adapter.py", encoding="utf-8"
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
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "antigravity-ide").mkdir()

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "degraded"
    assert check["details"]["hosts"]["cursor"]["status"] == "degraded"
    assert check["details"]["hosts"]["antigravity"]["status"] == "degraded"


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
    second_beat = threading.Event()
    real = doctor._heartbeat_maintenance_owner

    def heartbeat(*args, **kwargs):
        result = real(*args, **kwargs)
        beats.append(time.monotonic())
        if len(beats) >= 2:
            second_beat.set()
        return result

    monkeypatch.setattr(doctor, "MAINTENANCE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(doctor, "_heartbeat_maintenance_owner", heartbeat)

    def wait_for_two_heartbeats() -> None:
        # Each beat writes through the coordinator, so two of them cost far
        # more than the 10 ms interval on a loaded machine. The wait ends as
        # soon as the second beat lands; the budget is only its upper bound.
        assert second_beat.wait(timeout=120)

    with doctor._MaintenanceHeartbeat(
        coordinator, lease, deadline=time.monotonic() + 180
    ) as guard:
        guard.run(wait_for_two_heartbeats)

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

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        time_budget_seconds=30,
    )
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


def test_cli_returns_zero_for_healthy_report(tmp_path, monkeypatch, capsys):
    import doctor

    root, state_root, home = _build_root(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    (state_root / "run" / "state.json").write_text(
        json.dumps({"last_nightly_date": today, "last_nightly_status": "success"}),
        encoding="utf-8",
    )
    _create_index(state_root / "cache" / "index.sqlite")
    _create_claim_index(root, state_root)
    _create_generation(root, state_root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)

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


def test_cli_repair_json_is_idempotent(tmp_path, monkeypatch, capsys):
    import doctor

    root, _, home = _build_root(tmp_path)
    state_root = tmp_path / "missing-state"
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)
    clock = iter([0.0])
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock, 6.0))
    budgeted = ["--repair", "--json", "--time-budget", "30"]

    first_return_code = doctor.main(budgeted)
    first_report = json.loads(capsys.readouterr().out)
    after_first = _snapshot(state_root)
    second_return_code = doctor.main(budgeted)
    second_report = json.loads(capsys.readouterr().out)

    assert first_return_code == 0
    assert first_report["repaired"]
    assert first_report["overall_status"] == "ok"
    assert second_return_code == 0
    assert second_report["overall_status"] == "ok"
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

    assert report["overall_status"] in {"ok", "degraded"}, report
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
    clock = iter([0.0])
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock, 6.0))

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        time_budget_seconds=30,
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

    assert _check(report, "index")["status"] == "error", _check(report, "index")
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

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        time_budget_seconds=30,
    )

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
    assert check["details"].get("repair_deferred") is True, check
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
    clock = iter([0.0])
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock, 6.0))

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        time_budget_seconds=30,
    )

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
        assert check["details"].get("repair_deferred") is True, check
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


def test_run_doctor_uses_supplied_absolute_deadline_after_queue_delay(tmp_path, monkeypatch):
    import doctor

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
        return doctor._result("environment", "ok", "ok", {})

    def filesystem(_state_root, deadline, **kwargs):
        captured.append(deadline)
        raise RuntimeError("stop after deadline capture")

    monkeypatch.setattr(doctor.time, "monotonic", monotonic)
    monkeypatch.setattr(doctor, "_environment_check", delayed_environment)
    monkeypatch.setattr(
        doctor,
        "_runtime_check",
        lambda *args, **kwargs: doctor._result("runtime", "ok", "ok", {}),
    )
    monkeypatch.setattr(doctor, "_filesystem_check", filesystem)

    with pytest.raises(RuntimeError, match="deadline capture"):
        doctor.run_doctor(root=root, state_root=state, deadline=50.0)

    assert captured == [50.0]


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


def test_locked_index_returns_bounded_as_degraded(tmp_path):
    """The wait for a lock is bounded, so the check returns instead of blocking."""
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

    # The holder only releases after this call returns, so any finite time
    # proves the wait was bounded. The exact figure is the machine's, not ours;
    # the chosen wait itself is covered by the _read_busy_ms unit test.
    assert elapsed < 30
    assert check["status"] == "degraded"
    assert check["details"]["database_busy"] is True


def test_the_read_wait_survives_a_budgetless_deadline():
    """Most checks default to an infinite deadline; arithmetic must survive it."""
    import doctor

    assert doctor._read_busy_ms(float("inf")) == doctor.READ_BUSY_MS
    assert doctor._read_busy_ms(None) == doctor.READ_BUSY_MS
    assert doctor._read_busy_ms(time.monotonic() - 1) == 0
    assert 0 < doctor._read_busy_ms(time.monotonic() + 0.1) <= doctor.READ_BUSY_MS


def test_a_brief_commit_lock_does_not_make_a_healthy_index_look_busy(
    tmp_path, monkeypatch
):
    """A millisecond commit is normal; only a stuck database is worth reporting."""
    import doctor

    # The shipped wait is 250 ms; a loaded CI runner can hold a 50 ms lock for
    # longer than that, so the wait itself is what this test exercises.
    monkeypatch.setattr(doctor, "READ_BUSY_MS", 30_000)
    _, state_root, _ = _build_root(tmp_path)
    index = state_root / "cache" / "index.sqlite"
    _create_index(index)
    locked = threading.Event()
    released = threading.Event()

    def hold_briefly() -> None:
        writer = sqlite3.connect(index, isolation_level=None)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            locked.set()
            time.sleep(0.05)
            writer.execute("ROLLBACK")
        finally:
            writer.close()
        released.set()

    worker = threading.Thread(target=hold_briefly)
    worker.start()
    try:
        assert locked.wait(10)
        check = doctor._index_check(
            state_root,
            datetime.now(timezone.utc),
            deadline=time.monotonic() + 120.0,
        )
    finally:
        worker.join()

    assert released.is_set()
    assert check["details"].get("database_busy") is not True


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
        home / ".cursor" / "hooks.json": '{"version":1,"hooks":{}}',
        home / ".gemini" / "config" / "hooks.json": '{"team-hook":{}}',
    }
    for path, content in configs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["status"] == "degraded"
    assert check["details"]["hosts"]["claude"]["status"] == "degraded"
    assert check["details"]["hosts"]["opencode"]["status"] == "degraded"
    assert check["details"]["hosts"]["codex"]["status"] == "degraded"
    assert check["details"]["hosts"]["cursor"]["status"] == "degraded"
    assert check["details"]["hosts"]["antigravity"]["status"] == "degraded"


@pytest.mark.parametrize(
    ("host", "relative", "marker"),
    [
        ("claude", ".claude/settings.json", "LLM_WIKI_ROOT integration_adapter.py"),
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


@pytest.mark.parametrize(
    ("host", "relative", "expected"),
    [
        ("cursor", ".cursor/rules/llm-wiki.mdc", "degraded"),
        ("antigravity", ".gemini/antigravity/AGENTS.md", "skipped"),
    ],
)
def test_legacy_ide_markers_do_not_activate_managed_hooks(tmp_path, host, relative, expected):
    from doctor import run_doctor

    root, state_root, home = _build_root(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("LLM-Wiki LLM_WIKI_ROOT", encoding="utf-8")

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    assert check["details"]["hosts"][host]["status"] == expected


def test_managed_ide_hooks_report_active_structural_ownership(tmp_path):
    from doctor import run_doctor
    from integration_hook_config import managed_ide_hook_resources

    root, state_root, home = _build_root(tmp_path)
    (home / ".cursor").mkdir()
    (home / ".gemini" / "antigravity-ide").mkdir(parents=True)
    for resource in managed_ide_hook_resources(root, home):
        resource.write_owned(resource.desired)

    check = _check(run_doctor(root=root, state_root=state_root, home=home), "integrations")

    for host in ("cursor", "antigravity"):
        result = check["details"]["hosts"][host]
        assert result["status"] == "ok"
        assert result["configuration_status"] == "active"
        assert result["capture_mode"] == "official-user-hooks"


def _missing_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="missing",
        source=None,
        version=None,
        node_executable=None,
        node_version=None,
        node_major=None,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256="a" * 64,
        configuration_sha256="b" * 64,
        qualified=False,
        degradation_codes=("pyright_missing",),
    )


def _mismatched_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="degraded",
        source="system",
        version="1.1.400",
        node_executable=None,
        node_version=None,
        node_major=20,
        server_executable=None,
        executable_sha256=None,
        package_sha256=None,
        initialization_options_sha256=pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=pyright_profile.PYRIGHT_CONFIGURATION_SHA256,
        qualified=False,
        degradation_codes=("pyright_version_mismatch",),
    )


def _qualified_pyright_identity():
    import pyright_profile

    return pyright_profile.PyrightIdentity(
        status="qualified",
        source="project-local",
        version="1.1.411",
        node_executable=Path("node"),
        node_version="v20.19.0",
        node_major=20,
        server_executable=Path("langserver.index.js"),
        executable_sha256="c" * 64,
        package_sha256="d" * 64,
        initialization_options_sha256=pyright_profile.PYRIGHT_INITIALIZATION_OPTIONS_SHA256,
        configuration_sha256=pyright_profile.PYRIGHT_CONFIGURATION_SHA256,
        qualified=True,
        degradation_codes=(),
    )


def test_doctor_reports_missing_pyright(tmp_path, monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda *a, **k: _missing_pyright_identity(),
    )
    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)
    assert check["status"] == "degraded"
    assert check["details"]["status"] == "missing"
    assert check["details"]["codes"] == ["pyright_missing"]
    assert "install_pyright" in check["details"]["recommended_action"]


def test_doctor_pyright_passes_deadline_to_repository_scope(tmp_path, monkeypatch) -> None:
    import doctor

    deadline = time.monotonic() + 10
    observed: list[tuple[Path, float]] = []
    scope = object()

    def resolve(root: Path, *, deadline: float):
        observed.append((root, deadline))
        return scope

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: _missing_pyright_identity(),
    )

    doctor._pyright_check(tmp_path, tmp_path, deadline=deadline)

    assert observed == [(tmp_path, deadline)]


def test_doctor_pyright_maps_infinite_deadline_to_api_none(tmp_path, monkeypatch) -> None:
    import doctor
    import pyright_profile
    import repository_scope

    observed: list[tuple[str, float | None]] = []
    real_resolve = repository_scope.resolve_repository_scope
    real_discover = pyright_profile.discover_pyright
    no_candidates = pyright_profile.PyrightCandidates((), (), ())

    def resolve(root: Path, *, deadline: float | None):
        observed.append(("scope", deadline))
        return real_resolve(root, deadline=deadline)

    def discover(repository, *, state_root: Path, deadline: float | None):
        observed.append(("discovery", deadline))
        return real_discover(
            repository,
            state_root=state_root,
            candidates=no_candidates,
            deadline=deadline,
        )

    monkeypatch.setattr(repository_scope, "resolve_repository_scope", resolve)
    monkeypatch.setattr(pyright_profile, "discover_pyright", discover)

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert observed == [("scope", None), ("discovery", None)]
    assert check["details"]["status"] == "missing"
    assert check["details"]["codes"] == ["pyright_missing"]


def test_doctor_pyright_reports_executable_digest(tmp_path, monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: _qualified_pyright_identity(),
    )

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert check["details"]["executable_sha256"] == "c" * 64


def test_doctor_pyright_surfaces_executable_digest_mismatch(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    import doctor

    identity = replace(
        _qualified_pyright_identity(),
        status="degraded",
        source="managed",
        qualified=False,
        degradation_codes=("pyright_executable_digest_mismatch",),
    )
    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: identity,
    )

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=float("inf"))

    assert check["status"] == "degraded"
    assert check["details"]["codes"] == ["pyright_executable_digest_mismatch"]
    assert check["details"]["executable_sha256"] == "c" * 64


def test_doctor_pyright_reports_exact_degradation_codes_and_fields(tmp_path, monkeypatch) -> None:
    import doctor
    import pyright_profile

    codes = (
        "pyright_configuration_mismatch",
        "pyright_node_major_mismatch",
        "pyright_package_mismatch",
    )
    identity = pyright_profile.PyrightIdentity(
        status="degraded",
        source="managed",
        version="1.1.411",
        node_executable=Path("node"),
        node_version="v18.20.0",
        node_major=18,
        server_executable=Path("langserver.index.js"),
        executable_sha256="e" * 64,
        package_sha256="f" * 64,
        initialization_options_sha256="1" * 64,
        configuration_sha256="2" * 64,
        qualified=False,
        degradation_codes=codes,
    )
    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda repository, **kwargs: identity,
    )

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)

    assert check["status"] == "degraded"
    assert check["details"] == {
        "status": "degraded",
        "source": "managed",
        "version": "1.1.411",
        "node_major": 18,
        "node_version": "v18.20.0",
        "package_sha256": "f" * 64,
        "executable_sha256": "e" * 64,
        "initialization_options_sha256": "1" * 64,
        "configuration_sha256": "2" * 64,
        "qualified": False,
        "codes": list(codes),
        "executable_sha256_present": True,
        "recommended_action": (
            "uv run python scripts/install_pyright.py --state-root <state-root>"
        ),
    }


def test_doctor_pyright_reports_scope_timeout_separately(tmp_path, monkeypatch) -> None:
    import doctor

    def resolve(root: Path, *, deadline: float):
        raise TimeoutError("scope deadline")

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "timeout"
    assert check["details"]["codes"] == ["pyright_timeout"]


def test_doctor_pyright_reports_unsafe_scope_separately(tmp_path, monkeypatch) -> None:
    import doctor
    from repository_scope import RepositoryScopeUnavailable

    def resolve(root: Path, *, deadline: float):
        raise RepositoryScopeUnavailable("unsafe scope")

    monkeypatch.setattr("repository_scope.resolve_repository_scope", resolve)

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "unsafe"
    assert check["details"]["codes"] == ["pyright_unsafe"]


def test_doctor_pyright_reports_unsafe_managed_discovery_separately(tmp_path, monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )

    def discover(repository, **kwargs):
        raise PermissionError("managed Pyright root is unsafe")

    monkeypatch.setattr("pyright_profile.discover_pyright", discover)

    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)

    assert check["status"] == "degraded"
    assert check["details"]["status"] == "unsafe"
    assert check["details"]["codes"] == ["pyright_unsafe"]


def test_run_doctor_checks_pyright_without_managed_or_runtime_dirs(tmp_path, monkeypatch) -> None:
    import doctor

    root, state_root, home = _build_root(tmp_path)
    calls: list[tuple[Path, Path]] = []

    def pyright_check(root: Path, state_root: Path, *, deadline: float) -> dict:
        calls.append((root, state_root))
        return doctor._result(
            "pyright",
            "degraded",
            "Pyright is missing.",
            {"status": "missing", "codes": ["pyright_missing"]},
        )

    monkeypatch.setattr(doctor, "_pyright_check", pyright_check)

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )

    assert calls == [(root, state_root)]
    assert _check(report, "pyright")["details"]["codes"] == ["pyright_missing"]


def test_run_doctor_broken_lsp_link_blocks_run_deletion(tmp_path, monkeypatch) -> None:
    import doctor

    root, state_root, home = _build_root(tmp_path)
    lsp_root = state_root / "run" / "lsp"
    target = tmp_path / "removed-lsp-target"
    _broken_directory_link_or_skip(lsp_root, target)
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )
    lsp = _check(report, "lsp")

    assert lsp["status"] == "degraded"
    assert "lsp_state_unreadable" in lsp["details"]["codes"]
    assert report["run_deletion"]["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_run_doctor_reuses_collected_lsp_check_for_deletion(tmp_path, monkeypatch) -> None:
    import doctor

    root, state_root, home = _build_root(tmp_path)
    calls: list[Path] = []

    def lsp_check(state_root: Path, now: datetime, *, deadline: float) -> dict:
        calls.append(state_root)
        if len(calls) > 1:
            raise AssertionError("LSP runtime was scanned twice")
        return doctor._result(
            "lsp",
            "ok",
            "No LSP runtime owners are present.",
            {
                "codes": [],
                "owners": [],
                "deletion_codes": [],
                "read_error": False,
            },
        )

    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)
    monkeypatch.setattr(doctor, "_lsp_runtime_check", lsp_check)

    doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        time_budget_seconds=30,
    )

    assert calls == [state_root]


def test_run_doctor_executes_lsp_check_after_budget_exhaustion(tmp_path, monkeypatch) -> None:
    import doctor

    root, state_root, home = _build_root(tmp_path)
    calls: list[float] = []
    real_lsp_check = doctor._lsp_runtime_check

    def lsp_check(state_root: Path, now: datetime, *, deadline: float) -> dict:
        calls.append(deadline)
        return real_lsp_check(state_root, now, deadline=deadline)

    monkeypatch.setattr(doctor, "_lsp_runtime_check", lsp_check)
    deadline = time.monotonic() - 1

    report = doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        deadline=deadline,
    )

    assert calls == [deadline]
    assert _check(report, "lsp")["details"]["codes"] == ["lsp_state_unreadable"]
    assert report["run_deletion"]["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_doctor_reports_mismatched_pyright(tmp_path, monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        "repository_scope.resolve_repository_scope", lambda root, *, deadline: object()
    )
    monkeypatch.setattr(
        "pyright_profile.discover_pyright",
        lambda *a, **k: _mismatched_pyright_identity(),
    )
    check = doctor._pyright_check(tmp_path, tmp_path, deadline=time.monotonic() + 10)
    assert check["status"] == "degraded"
    assert "pyright_version_mismatch" in check["details"]["codes"]


def test_doctor_lsp_check_reports_no_owners_when_absent(tmp_path) -> None:
    import doctor

    check = doctor._lsp_runtime_check(tmp_path, datetime.now(timezone.utc), deadline=float("inf"))
    assert check["status"] == "ok"
    assert check["details"]["owners"] == []


def _lsp_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_lsp_owner(
    state_root: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    started_at: datetime,
    owner_pid: int,
) -> Path:
    owner = state_root / "run" / "lsp" / owner_nonce
    owner.mkdir(parents=True)
    (owner / "cancellation").mkdir()
    (owner / "owner.json").write_text(
        json.dumps(
            {
                "command_basename": "pyright-langserver",
                "generation_nonce": generation_nonce,
                "owner_nonce": owner_nonce,
                "owner_pid": owner_pid,
                "started_at": _lsp_timestamp(started_at),
                "state": "process_running",
            }
        ),
        encoding="utf-8",
    )
    return owner


def _write_lsp_lease(
    owner: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    manager_pid: int,
    server_pid: int,
    heartbeat_at: datetime,
    expires_at: datetime,
) -> None:
    (owner / "lease.json").write_text(
        json.dumps(
            {
                "expires_at": _lsp_timestamp(expires_at),
                "generation_nonce": generation_nonce,
                "heartbeat_at": _lsp_timestamp(heartbeat_at),
                "manager_pid": manager_pid,
                "owner_nonce": owner_nonce,
                "schema_version": 1,
                "server_pid": server_pid,
                "state": "live",
            }
        ),
        encoding="utf-8",
    )


def _write_lsp_failure(
    owner: Path,
    *,
    owner_nonce: str,
    generation_nonce: str,
    timestamp: datetime,
    server_pid: int | None = None,
) -> None:
    record: dict[str, object] = {
        "code": "process_exited",
        "generation_nonce": generation_nonce,
        "owner_nonce": owner_nonce,
        "timestamp": _lsp_timestamp(timestamp),
    }
    if server_pid is not None:
        record["server_pid"] = server_pid
    (owner / "failure.json").write_text(json.dumps(record), encoding="utf-8")


def test_doctor_lsp_record_read_checks_absolute_deadline_around_handle_io(
    monkeypatch,
) -> None:
    import doctor

    deadline = time.monotonic() + 10
    observed_deadlines: list[float] = []

    class Entry:
        name = "owner.json"
        kind = "file"
        file_id = b"i" * 16
        size = 2

    class Workspace:
        closed: list[int] = []

        @staticmethod
        def open_file(parent: int, name: str) -> int:
            assert (parent, name) == (7, "owner.json")
            return 8

        @staticmethod
        def identity(handle: int, *, directory: bool):
            assert (handle, directory) == (8, False)
            return 1, Entry.file_id, False

        @staticmethod
        def file_size(handle: int) -> int:
            assert handle == 8
            return 2

        @staticmethod
        def read_chunks(handle: int, *, chunk_bytes: int, max_bytes: int):
            assert (handle, chunk_bytes, max_bytes) == (8, 4096, 64 * 1024)
            yield b"{}"

        @classmethod
        def close_handle(cls, handle: int) -> None:
            cls.closed.append(handle)

    def deadline_reached(value: float) -> bool:
        observed_deadlines.append(value)
        return False

    monkeypatch.setattr(doctor, "_deadline_reached", deadline_reached)

    assert doctor._read_windows_lsp_record(Workspace, 7, Entry(), deadline) == {}
    assert observed_deadlines
    assert set(observed_deadlines) == {deadline}
    assert Workspace.closed == [8]


def test_doctor_lsp_record_disappearing_after_scan_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    if os.name == "nt":
        real_read = doctor._read_windows_lsp_record

        def read(workspace, owner_handle, entry, deadline):
            if entry.name == "owner.json":
                raise FileNotFoundError(entry.name)
            return real_read(workspace, owner_handle, entry, deadline)

        monkeypatch.setattr(doctor, "_read_windows_lsp_record", read)
    else:
        real_read = doctor._read_posix_lsp_record

        def read(owner_fd, name, deadline):
            if name == "owner.json":
                raise FileNotFoundError(name)
            return real_read(owner_fd, name, deadline)

        monkeypatch.setattr(doctor, "_read_posix_lsp_record", read)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_path_swap_reads_only_retained_tree(tmp_path, monkeypatch) -> None:
    import doctor
    import windows_workspace

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    old_nonce = "a" * 32
    replacement_nonce = "b" * 32
    _write_lsp_owner(
        state_root,
        owner_nonce=old_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=1111,
    )
    replacement_state = tmp_path / "replacement-state"
    _write_lsp_owner(
        replacement_state,
        owner_nonce=replacement_nonce,
        generation_nonce="2" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    lsp_root = state_root / "run" / "lsp"
    replacement = replacement_state / "run" / "lsp"
    displaced = state_root / "run" / "displaced-lsp"
    swapped = False

    def swap_path() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        try:
            lsp_root.rename(displaced)
            replacement.rename(lsp_root)
        except OSError:
            # Windows retained handles may deny the rename, which is also safe.
            return

    real_scandir = os.scandir

    def scandir(path):
        if isinstance(path, int) or Path(path) == lsp_root:
            swap_path()
        return real_scandir(path)

    real_list_directory = windows_workspace.list_directory

    def list_directory(handle: int, *, max_entries: int):
        swap_path()
        return real_list_directory(handle, max_entries=max_entries)

    monkeypatch.setattr(doctor.os, "scandir", scandir)
    monkeypatch.setattr(windows_workspace, "list_directory", list_directory)

    check = doctor._lsp_runtime_check(
        state_root,
        now,
        deadline=time.monotonic() + 10,
    )

    assert [owner["owner_nonce"] for owner in check["details"]["owners"]] == [old_nonce]


def test_doctor_lsp_production_live_lease_blocks_deletion(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(
        doctor,
        "_lsp_pid_state",
        lambda pid: "alive" if pid in {1111, 2222} else "dead",
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=time.monotonic() + 10)
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert check["details"]["codes"] == ["lsp_owner_live"]
    assert deletion["quiescent"] is False
    assert deletion["permit"] is False
    assert deletion["permit"] is False


def test_doctor_lsp_pid_probe_deadline_crossing_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    clock = [1.0]
    deadline = 2.0
    probed: list[int] = []

    def pid_state(pid: int) -> str:
        probed.append(pid)
        clock[0] = deadline
        return "alive"

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor, "_lsp_pid_state", pid_state)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=deadline)
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert probed == [1111]
    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_doctor_lsp_unknown_pid_probe_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        doctor,
        "_lsp_pid_state",
        lambda pid: "unknown",
        raising=False,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=time.monotonic() + 10)
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert "lsp_owner_live" not in check["details"]["codes"]
    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert deletion["quiescent"] is False


def test_doctor_lsp_posix_eperm_pid_probe_is_unknown(monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(doctor.sys, "platform", "linux")

    def denied(pid: int, signal: int) -> None:
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(doctor.os, "kill", denied)

    assert doctor._lsp_pid_state(1234) == "unknown"


def test_doctor_lsp_dead_owner_uses_last_heartbeat_as_crash_evidence(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(days=1),
        expires_at=now - timedelta(days=1) + timedelta(seconds=30),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert check["details"]["codes"] == ["lsp_failure_evidence_retained"]
    assert check["details"]["owners"][0]["failure_age_days"] == pytest.approx(1)


def test_doctor_lsp_crash_evidence_expires_at_exact_seven_day_boundary(
    tmp_path, monkeypatch
) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(days=7),
        expires_at=now - timedelta(days=7) + timedelta(seconds=30),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert check["details"]["codes"] == []
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize(
    ("failure_age", "retained"),
    [
        (timedelta(days=7) - timedelta(seconds=1), True),
        (timedelta(days=7), False),
    ],
)
def test_doctor_lsp_failure_retention_exact_seven_day_boundary(
    tmp_path, failure_age, retained
) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - failure_age,
        server_pid=2222,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert ("lsp_failure_evidence_retained" in check["details"]["codes"]) is retained
    assert deletion["quiescent"] is False


def test_doctor_lsp_failure_only_owner_without_owner_record_fails_closed(
    tmp_path,
) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    owner = tmp_path / "run" / "lsp" / owner_nonce
    owner.mkdir(parents=True)
    (owner / "cancellation").mkdir()
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        timestamp=now - timedelta(days=8),
        server_pid=2222,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


def test_doctor_lsp_dead_owner_without_lease_uses_owner_start_time(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert check["details"]["codes"] == []
    assert check["details"]["owners"][0]["failure_age_days"] == pytest.approx(8)


def test_doctor_lsp_malformed_record_fails_closed(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    (owner / "lease.json").write_text("{", encoding="utf-8")

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize("duplicate_key", ["owner_nonce", "timestamp"])
def test_doctor_lsp_rejects_duplicate_json_keys(tmp_path, duplicate_key) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    timestamp = _lsp_timestamp(now - timedelta(days=8))
    if duplicate_key == "owner_nonce":
        (owner / "owner.json").write_text(
            "{"
            '"command_basename":"pyright-langserver",'
            f'"generation_nonce":"{generation_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            '"owner_pid":2222,'
            f'"started_at":"{timestamp}",'
            '"state":"process_running"'
            "}",
            encoding="utf-8",
        )
    else:
        (owner / "failure.json").write_text(
            "{"
            '"code":"process_exited",'
            f'"generation_nonce":"{generation_nonce}",'
            f'"owner_nonce":"{owner_nonce}",'
            f'"timestamp":"{timestamp}",'
            f'"timestamp":"{timestamp}",'
            '"server_pid":2222'
            "}",
            encoding="utf-8",
        )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert deletion["quiescent"] is False
    assert deletion["quiescent"] is False
    assert deletion["permit"] is False


def test_doctor_lsp_deep_valid_size_json_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor

    payload = b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    assert len(payload) <= 64 * 1024

    def snapshot(state_root: Path, deadline: float):
        try:
            doctor._decode_lsp_record(payload)
        except (UnicodeError, ValueError):
            return [], True, False
        return [], False, False

    monkeypatch.setattr(doctor, "_snapshot_lsp_runtime", snapshot)

    original_recursion_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(max(original_recursion_limit, 3_000))
        check = doctor._lsp_runtime_check(
            tmp_path,
            datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
            deadline=float("inf"),
        )
    finally:
        sys.setrecursionlimit(original_recursion_limit)

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_json_depth_ignores_delimiters_inside_strings() -> None:
    import doctor

    value = '[{"escaped":"\\""}]' * 64
    payload = json.dumps({"value": value}).encode("utf-8")

    assert doctor._decode_lsp_record(payload) == {"value": value}


def test_doctor_lsp_json_depth_accepts_exact_limit() -> None:
    import doctor

    array_depth = doctor._LSP_JSON_MAX_DEPTH - 1
    payload = b'{"nested":' + b"[" * array_depth + b"0" + b"]" * array_depth + b"}"

    assert isinstance(doctor._decode_lsp_record(payload), dict)


@pytest.mark.parametrize("record_name", ["owner.json", "lease.json", "failure.json"])
def test_doctor_lsp_rejects_nonproduction_record_schema(tmp_path, monkeypatch, record_name) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    if record_name == "lease.json":
        _write_lsp_lease(
            owner,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            manager_pid=1111,
            server_pid=2222,
            heartbeat_at=now - timedelta(seconds=5),
            expires_at=now + timedelta(seconds=25),
        )
    elif record_name == "failure.json":
        _write_lsp_failure(
            owner,
            owner_nonce=owner_nonce,
            generation_nonce=generation_nonce,
            timestamp=now - timedelta(days=8),
            server_pid=2222,
        )
    path = owner / record_name
    record = json.loads(path.read_text(encoding="utf-8"))
    record["unexpected"] = True
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_rejects_non_integer_lease_schema_version(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    lease_path = owner / "lease.json"
    lease = json.loads(lease_path.read_text(encoding="utf-8"))
    lease["schema_version"] = 1.0
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    monkeypatch.setattr(doctor, "_lsp_pid_state", lambda _pid: "alive")

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert "lsp_owner_live" not in check["details"]["codes"]


def test_doctor_lsp_rejects_lease_generation_identity_mismatch(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="2" * 32,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert "lsp_owner_live" not in check["details"]["codes"]


def test_doctor_lsp_rejects_failure_generation_identity_mismatch(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=9),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce="2" * 32,
        timestamp=now - timedelta(days=8),
        server_pid=2222,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_rejects_lease_heartbeat_before_owner_start(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(seconds=25),
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert "lsp_owner_live" not in check["details"]["codes"]


def test_doctor_lsp_rejects_failure_before_owner_start(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=1),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=2),
        server_pid=2222,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_rejects_future_owner_start(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now + timedelta(seconds=1),
        owner_pid=2222,
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_unknown_owner_entry_fails_closed(tmp_path) -> None:
    import doctor

    unknown = tmp_path / "run" / "lsp" / "unexpected"
    unknown.mkdir(parents=True)

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=float("inf"),
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_129th_owner_entry_marks_scan_truncated(tmp_path) -> None:
    import doctor

    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.mkdir(parents=True)
    for index in range(129):
        (lsp_root / f"{index:032x}").mkdir()

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=float("inf"),
    )

    assert len(check["details"]["owners"]) <= 128
    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_preexpired_deadline_fails_closed(tmp_path) -> None:
    import doctor

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=time.monotonic() - 1,
    )

    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_absent_with_expired_deadline_fails_before_lstat(tmp_path, monkeypatch) -> None:
    import doctor

    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: (_ for _ in ()).throw(AssertionError("lstat after deadline")),
    )
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)

    check = doctor._lsp_runtime_check(
        tmp_path / "absent-state",
        now,
        deadline=time.monotonic() - 1,
    )
    deletion = doctor._run_deletion_check(
        tmp_path,
        now,
        collected={
            "transactions": {"details": {"deletion_codes": []}},
            "queue": {"details": {"deletion_codes": []}},
            "archives": {"details": {"deletion_codes": []}},
            "lsp": check,
        },
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["quiescent"] is False
    assert deletion["permit"] is False


def test_doctor_posix_lsp_missing_root_rechecks_deadline(tmp_path, monkeypatch) -> None:
    import doctor

    clock = [1.0]
    deadline = 2.0

    def open_root(state_root: Path, observed_deadline: float) -> int:
        assert (state_root, observed_deadline) == (tmp_path, deadline)
        clock[0] = deadline
        raise FileNotFoundError(state_root)

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(doctor, "_open_posix_lsp_root", open_root)

    assert doctor._snapshot_posix_lsp(tmp_path, deadline) == ([], True, False)


def test_doctor_windows_lsp_missing_root_rechecks_deadline(tmp_path, monkeypatch) -> None:
    import doctor
    import windows_workspace

    clock = [1.0]
    deadline = 2.0

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        clock[0] = deadline
        raise FileNotFoundError(path)

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(windows_workspace, "open_directory_path", open_root)

    assert doctor._snapshot_windows_lsp(tmp_path, deadline) == ([], True, False)


def test_doctor_windows_lsp_closes_root_when_deadline_expires_after_open(
    tmp_path, monkeypatch
) -> None:
    import doctor
    import windows_workspace

    clock = [1.0]
    deadline = 2.0
    closed: list[int] = []

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        clock[0] = deadline
        return 73

    monkeypatch.setattr(doctor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(windows_workspace, "open_directory_path", open_root)
    monkeypatch.setattr(windows_workspace, "close_handle", closed.append)

    assert doctor._snapshot_windows_lsp(tmp_path, deadline) == ([], True, False)
    assert closed == [73]


@pytest.mark.parametrize("error_type", [RuntimeError, NotImplementedError])
@pytest.mark.parametrize(
    ("failure_stage", "expected_closed"),
    [
        ("open_root", []),
        ("list_root", [10]),
        ("owner_identity", [20, 10]),
        ("cancellation_identity", [30, 20, 10]),
        ("record_read", [40, 20, 10]),
    ],
)
def test_doctor_windows_lsp_capability_failures_close_retained_handles(
    tmp_path, monkeypatch, error_type, failure_stage, expected_closed
) -> None:
    import doctor
    import windows_workspace as workspace

    owner_name = "a" * 32
    owner_id = b"o" * 16
    cancellation_id = b"c" * 16
    record_id = b"r" * 16
    payload = b"{}"
    owner_entry = workspace.WindowsEntry(owner_name, "directory", owner_id)
    cancellation_entry = workspace.WindowsEntry("cancellation", "directory", cancellation_id)
    record_entry = workspace.WindowsEntry("owner.json", "file", record_id, len(payload))
    closed: list[int] = []

    def fail() -> None:
        raise error_type("Windows workspace capability unavailable")

    def open_root(path: Path) -> int:
        assert path == tmp_path / "run" / "lsp"
        if failure_stage == "open_root":
            fail()
        return 10

    def list_directory(handle: int, *, max_entries: int):
        if handle == 10:
            if failure_stage == "list_root":
                fail()
            return [owner_entry]
        assert (handle, max_entries) == (20, len(doctor._LSP_OWNER_ENTRY_NAMES))
        if failure_stage == "cancellation_identity":
            return [cancellation_entry]
        return [record_entry]

    def open_directory(parent: int, name: str) -> int:
        if parent == 10:
            assert name == owner_name
            return 20
        assert (parent, name) == (20, "cancellation")
        return 30

    def identity(handle: int, *, directory: bool | None = None):
        if handle == 20:
            if failure_stage == "owner_identity":
                fail()
            return 1, owner_id, True
        if handle == 30:
            if failure_stage == "cancellation_identity":
                fail()
            return 1, cancellation_id, True
        assert (handle, directory) == (40, False)
        return 1, record_id, False

    def read_chunks(handle: int, *, chunk_bytes: int, max_bytes: int):
        assert handle == 40
        if failure_stage == "record_read":
            fail()
        return [payload]

    monkeypatch.setattr(workspace, "open_directory_path", open_root)
    monkeypatch.setattr(workspace, "list_directory", list_directory)
    monkeypatch.setattr(workspace, "open_directory", open_directory)
    monkeypatch.setattr(workspace, "identity", identity)
    monkeypatch.setattr(workspace, "open_file", lambda parent, name: 40)
    monkeypatch.setattr(workspace, "file_size", lambda handle: len(payload))
    monkeypatch.setattr(workspace, "read_chunks", read_chunks)
    monkeypatch.setattr(workspace, "close_handle", closed.append)

    _snapshots, unreadable, absent = doctor._snapshot_windows_lsp(tmp_path, float("inf"))

    assert unreadable is True
    assert absent is False
    assert closed == expected_closed


def test_doctor_lsp_deadline_expiring_during_scan_fails_closed(tmp_path, monkeypatch) -> None:
    import doctor

    owner = tmp_path / "run" / "lsp" / ("a" * 32)
    owner.mkdir(parents=True)
    observations = iter((False, True))
    monkeypatch.setattr(
        doctor,
        "_deadline_reached",
        lambda deadline: next(observations, True),
    )

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=1.0,
    )

    assert check["details"]["owners"] == []
    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_root_fails_closed(tmp_path, link_kind) -> None:
    import doctor

    external = tmp_path / "external"
    external.mkdir()
    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.parent.mkdir(parents=True)
    if link_kind == "symlink":
        _symlink_or_skip(lsp_root, external, directory=True)
    else:
        _junction_or_skip(lsp_root, external)

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=float("inf"),
    )

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_external_run_junction_fails_closed(tmp_path) -> None:
    import doctor

    state_root = tmp_path / "state"
    state_root.mkdir()
    external_run = tmp_path / "external-run"
    (external_run / "lsp").mkdir(parents=True)
    run_root = state_root / "run"
    if os.name == "nt":
        _junction_or_skip(run_root, external_run)
    else:
        _symlink_or_skip(run_root, external_run, directory=True)

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    check = doctor._lsp_runtime_check(state_root, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(state_root, now, collected={"lsp": check})

    assert check["details"]["codes"] == ["lsp_state_unreadable"]
    assert deletion["blockers"] == [{"code": "legacy_protocol_unquiesced"}]


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_owner_fails_closed(tmp_path, link_kind) -> None:
    import doctor

    external = tmp_path / "external-owner"
    external.mkdir()
    lsp_root = tmp_path / "run" / "lsp"
    lsp_root.mkdir(parents=True)
    owner = lsp_root / ("a" * 32)
    if link_kind == "symlink":
        _symlink_or_skip(owner, external, directory=True)
    else:
        _junction_or_skip(owner, external)

    check = doctor._lsp_runtime_check(
        tmp_path,
        datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
        deadline=float("inf"),
    )

    assert "lsp_state_unreadable" in check["details"]["codes"]
    assert check["details"]["read_error"] is True


def test_doctor_lsp_symlink_record_entry_fails_closed(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    external = tmp_path / "external-lease.json"
    external.write_text("{}", encoding="utf-8")
    _symlink_or_skip(owner / "lease.json", external)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


@pytest.mark.parametrize("link_kind", ["symlink", "junction"])
def test_doctor_lsp_linked_cancellation_directory_fails_closed(tmp_path, link_kind) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    cancellation = owner / "cancellation"
    cancellation.rmdir()
    external = tmp_path / "external-cancellation"
    external.mkdir()
    if link_kind == "symlink":
        _symlink_or_skip(cancellation, external, directory=True)
    else:
        _junction_or_skip(cancellation, external)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_owner_child_scan_stops_at_fifth_entry(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    for index in range(5):
        (owner / f"unexpected-{index}").write_text("", encoding="utf-8")
    real_scandir = os.scandir
    observed_windows_limits: list[int] = []

    class BoundedScan:
        def __init__(self, path) -> None:
            self.scanned = real_scandir(path)
            self.count = 0

        def __enter__(self):
            self.scanned.__enter__()
            return self

        def __exit__(self, *args):
            return self.scanned.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            self.count += 1
            if self.count > 5:
                raise AssertionError("owner child scan consumed a sixth entry")
            return next(self.scanned)

    def scandir(path):
        if isinstance(path, int) and os.path.samestat(os.fstat(path), owner.stat()):
            return BoundedScan(path)
        return real_scandir(path)

    if os.name == "nt":
        import windows_workspace

        real_list_directory = windows_workspace.list_directory

        def list_directory(handle: int, *, max_entries: int):
            observed_windows_limits.append(max_entries)
            return real_list_directory(handle, max_entries=max_entries)

        monkeypatch.setattr(windows_workspace, "list_directory", list_directory)
    else:
        monkeypatch.setattr(doctor.os, "scandir", scandir)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]
    if os.name == "nt":
        assert 4 in observed_windows_limits


def test_doctor_lsp_unknown_owner_child_fails_closed(tmp_path) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce="a" * 32,
        generation_nonce="1" * 32,
        started_at=now - timedelta(days=8),
        owner_pid=2222,
    )
    (owner / "unexpected.json").write_text("{}", encoding="utf-8")

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))

    assert "lsp_state_unreadable" in check["details"]["codes"]


def test_doctor_lsp_live_owner_and_failure_block_deletion(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    live_owner_nonce = "a" * 32
    live_generation_nonce = "1" * 32
    live_owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=live_owner_nonce,
        generation_nonce=live_generation_nonce,
        started_at=now - timedelta(minutes=1),
        owner_pid=2222,
    )
    _write_lsp_lease(
        live_owner,
        owner_nonce=live_owner_nonce,
        generation_nonce=live_generation_nonce,
        manager_pid=1111,
        server_pid=2222,
        heartbeat_at=now - timedelta(seconds=5),
        expires_at=now + timedelta(seconds=25),
    )
    failure_owner_nonce = "b" * 32
    failure_generation_nonce = "2" * 32
    failure_owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=failure_owner_nonce,
        generation_nonce=failure_generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=3333,
    )
    _write_lsp_failure(
        failure_owner,
        owner_nonce=failure_owner_nonce,
        generation_nonce=failure_generation_nonce,
        timestamp=now - timedelta(days=1),
        server_pid=3333,
    )
    monkeypatch.setattr(
        doctor,
        "_lsp_pid_state",
        lambda pid: "alive" if pid in {1111, 2222} else "dead",
    )

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})
    blocker_codes = {item["code"] for item in deletion["blockers"]}
    assert blocker_codes == {"legacy_protocol_unquiesced"}
    assert deletion["quiescent"] is False


def test_doctor_lsp_expired_failure_does_not_block(tmp_path, monkeypatch) -> None:
    import doctor

    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "c" * 32
    generation_nonce = "3" * 32
    owner = _write_lsp_owner(
        tmp_path,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=9),
        owner_pid=3333,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=8),
        server_pid=3333,
    )
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: False)

    check = doctor._lsp_runtime_check(tmp_path, now, deadline=float("inf"))
    deletion = doctor._run_deletion_check(tmp_path, now, collected={"lsp": check})
    assert "lsp_failure_evidence_retained" not in {item["code"] for item in deletion["blockers"]}


def test_doctor_repair_preserves_lsp_runtime_bytes(tmp_path, monkeypatch) -> None:
    import doctor

    root, state_root, home = _build_root(tmp_path)
    now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
    owner_nonce = "a" * 32
    generation_nonce = "1" * 32
    owner = _write_lsp_owner(
        state_root,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        started_at=now - timedelta(days=2),
        owner_pid=2222,
    )
    _write_lsp_failure(
        owner,
        owner_nonce=owner_nonce,
        generation_nonce=generation_nonce,
        timestamp=now - timedelta(days=1),
        server_pid=2222,
    )
    lsp_root = state_root / "run" / "lsp"
    before = _snapshot(lsp_root)
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)

    doctor.run_doctor(
        root=root,
        state_root=state_root,
        home=home,
        repair=True,
        repair_actions={"runtime"},
        now=now,
        time_budget_seconds=30,
    )

    assert _snapshot(lsp_root) == before


def test_lost_captures_are_reported_as_a_degraded_capture_check(tmp_path):
    """A capture the hooks lost must show up in health, not only at session start."""
    import doctor

    root, state_root, home = _build_root(tmp_path)
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    (state_root / "run" / "state.json").write_text(
        json.dumps(
            {
                "capture_failures": {
                    "session_end": {"count": 2, "last_reason": "spawn_failed"},
                    "pre_compact": {"count": 1, "last_reason": "spawn_failed"},
                }
            }
        ),
        encoding="utf-8",
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "degraded"
    assert check["details"]["lost"] == 3
    assert check["details"]["kinds"] == {"session_end": 2, "pre_compact": 1}
    assert "capture-failures.jsonl" in check["details"]["trail"]
    del home


def test_a_vault_without_lost_captures_reports_the_capture_check_as_ok(tmp_path):
    import doctor

    root, state_root, home = _build_root(tmp_path)

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "capture")

    assert check["status"] == "ok"
    assert check["details"]["lost"] == 0
    del home

def test_a_vault_written_to_mid_capture_is_captured_again(tmp_path, monkeypatch):
    """An active vault changes while it is read; deferring would never refresh it."""
    import doctor
    from corpus_snapshot import CorpusChanged

    calls: list[bool] = []

    def refresh(root, state, coordinator, lease, deadline, max_sources, force, repaired):
        calls.append(force)
        if len(calls) == 1:
            raise CorpusChanged("live corpus membership or source hashes changed")
        return {"status": "built", "generation_id": "gen-2"}

    monkeypatch.setattr(doctor, "_refreshed_generation", refresh)
    monkeypatch.setattr(
        doctor, "_acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(doctor, "_release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_unusable_filesystem_outcome", lambda *a, **k: None)

    result = doctor.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "built"
    assert calls == [False, True]

def test_losing_the_maintenance_fence_is_reported_not_raised(tmp_path, monkeypatch):
    """The nightly timer and a manual run collide; the loser reports, not crashes."""
    import doctor

    def refresh(*_args, **_kwargs):
        raise RuntimeError("maintenance_owner_fence_lost")

    monkeypatch.setattr(doctor, "_refreshed_generation", refresh)
    monkeypatch.setattr(
        doctor, "_acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(doctor, "_release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_unusable_filesystem_outcome", lambda *a, **k: None)

    result = doctor.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "deferred"
    assert result["reason"] == "maintenance_owner_lost"


def test_an_unrelated_runtime_error_still_escapes(tmp_path, monkeypatch):
    """Only the fence loss is an outcome; anything else is still a defect."""
    import doctor

    def refresh(*_args, **_kwargs):
        raise RuntimeError("something else went wrong")

    monkeypatch.setattr(doctor, "_refreshed_generation", refresh)
    monkeypatch.setattr(
        doctor, "_acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(doctor, "_release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_unusable_filesystem_outcome", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="something else"):
        doctor.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

def test_losing_the_fence_during_the_recapture_is_also_reported(tmp_path, monkeypatch):
    """The recapture runs inside an except block, out of reach of its handlers."""
    import doctor
    from corpus_snapshot import CorpusChanged

    calls: list[bool] = []

    def refresh(root, state, coordinator, lease, deadline, max_sources, force, repaired):
        calls.append(force)
        if len(calls) == 1:
            raise CorpusChanged("live corpus membership or source hashes changed")
        raise RuntimeError("maintenance_owner_fence_lost")

    monkeypatch.setattr(doctor, "_refreshed_generation", refresh)
    monkeypatch.setattr(
        doctor, "_acquire_maintenance_owner", lambda *a, **k: (object(), object())
    )
    monkeypatch.setattr(doctor, "_release_maintenance_owner", lambda *a, **k: None)
    monkeypatch.setattr(doctor, "_unusable_filesystem_outcome", lambda *a, **k: None)

    result = doctor.run_generation_maintenance(tmp_path, tmp_path, time_budget_seconds=30)

    assert result["status"] == "deferred"
    assert result["reason"] == "maintenance_owner_lost"
    assert calls == [False, True]

def test_the_host_markers_are_ones_the_shipped_templates_actually_write() -> None:
    """A marker naming a script the installer no longer writes fails every install."""
    import doctor

    root = Path(__file__).resolve().parent.parent
    shipped = {
        "claude": root / "integrations" / "claude-code" / "settings.json",
    }
    configs = doctor._integration_host_configs(Path.home())
    missing: dict[str, list[str]] = {}
    for name, template in shipped.items():
        text = template.read_text(encoding="utf-8")
        _host_dir, files = configs[name]
        for _path, markers in files:
            absent = [marker for marker in markers if marker not in text]
            if absent:
                missing[name] = absent

    assert missing == {}
