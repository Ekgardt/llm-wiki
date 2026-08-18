from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = ROOT / "scripts" / "sync_memory.py"
EXPECTED_ACTIONS = (
    "environment",
    "dependencies",
    "integrations",
    "transactions",
    "queue",
    "indexes",
    "doctor",
)


def _load_sync_memory():
    spec = importlib.util.spec_from_file_location("sync_memory_under_test", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree_paths(tree, *, read_only: bool) -> list:
    """Children first when locking, parent first when unlocking."""
    if read_only:
        return [*tree.rglob("*"), tree]
    return [tree, *tree.rglob("*")]


def _entry_mode(path, *, read_only: bool) -> int:
    if path.is_dir():
        return 0o555 if read_only else 0o755
    return 0o444 if read_only else 0o644


def _set_tree_mode(tree, *, read_only: bool) -> None:
    """Flip a whole tree between read-only and writable, best effort."""
    for path in _tree_paths(tree, read_only=read_only):
        _chmod_quietly(path, _entry_mode(path, read_only=read_only))


def _chmod_quietly(path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _sync_apply_line(script: str) -> str:
    return next(
        line
        for line in script.splitlines()
        if "sync_memory.py" in line and "--apply" in line
    )


def _apply_knowledge_change(root, original, change: str) -> None:
    """One way the knowledge tree can differ from the built index."""
    if change == "added":
        added = root / "knowledge" / "notes" / "added.md"
        added.write_text("---\ntype: concept\n---\n# Added\n", encoding="utf-8")
        return
    if change == "removed":
        original.unlink()
        return
    original.write_text("---\ntype: concept\n---\n# Modified\n", encoding="utf-8")


def _damage_manifest(manifest, manifest_state: str) -> None:
    if manifest_state == "missing":
        manifest.unlink()
        return
    manifest.write_text("{not-json", encoding="utf-8")


def _assert_stale_index_action(report, apply: bool) -> None:
    """Check mode reports the staleness; apply mode rebuilds it."""
    action = next(item for item in report["actions"] if item["id"] == "indexes")
    assert action["status"] == ("changed" if apply else "skipped"), action
    if apply:
        return
    assert action["details"]["freshness"] == "stale"
    assert action["details"]["source_rebuild_required"] is True


def _check_details(check_id: str, status: str) -> dict[str, object]:
    if check_id != "index":
        return {}
    return {
        "freshness": "fresh" if status == "ok" else "missing",
        "repairable": status == "degraded",
    }


def _action_ids(report) -> tuple[str, ...]:
    return tuple(action["id"] for action in report["actions"])


def _all_actions_ok(report) -> bool:
    return all(action["status"] == "ok" for action in report["actions"])


def _all_dry_run(calls) -> bool:
    return all(call["repair"] is False for call in calls)


def _doctor_report(
    *,
    repaired: list[dict] | None = None,
    environment: str = "ok",
    integrations: str = "ok",
    transactions: str = "ok",
    queue: str = "ok",
    index: str = "ok",
    overall: str = "ok",
) -> dict:
    statuses = {
        "environment": environment,
        "runtime": environment,
        "filesystem": environment,
        "integrations": integrations,
        "transactions": transactions,
        "queue": queue,
        "index": index,
    }
    return {
        "overall_status": overall,
        "repaired": repaired or [],
        "checks": [
            {
                "id": check_id,
                "status": status,
                "message": f"{check_id} is {status}",
                "details": _check_details(check_id, status),
            }
            for check_id, status in statuses.items()
        ],
    }


def _dependency_result(status: str = "ok") -> dict:
    return {
        "id": "dependencies",
        "status": status,
        "message": "Dependencies are synchronized.",
        "details": {"lock": "current", "environment": "current"},
    }


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def _build_vault(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    home = tmp_path / "home"
    for path in (
        root / "knowledge" / "notes",
        root / "scripts",
        root / "integrations" / "claude-code",
        root / "integrations" / "cursor" / "rules",
        root / "integrations" / "antigravity",
        state / "run" / "queue",
        state / "logs",
        state / "cache",
        home,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "notes" / "example.md").write_text(
        "---\ntype: concept\n---\n# Example\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.10"\n'
        'dependencies = ["mcp>=1.29,<2"]\n'
        '[project.optional-dependencies]\nmcp-server = []\n',
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.10"\n'
        '[[package]]\nname = "mcp"\nversion = "1.29.0"\n'
        '[[package]]\nname = "llm-wiki"\nprovides-extras = ["mcp-server"]\n',
        encoding="utf-8",
    )
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
    return root, state, home


def _create_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE VIRTUAL TABLE pages USING fts5("
            "path UNINDEXED, title, summary, body, project UNINDEXED, "
            "timestamp UNINDEXED, slug)"
        )


def _build_fresh_search_index(sync_memory, root: Path, state: Path) -> None:
    result = sync_memory._run_index_builder(
        root=root,
        state_root=state,
        timeout=5,
    )
    assert result["status"] == "changed"


def test_check_is_default_dry_run_and_actions_are_ordered(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []
    monkeypatch.setattr(
        sync_memory.doctor,
        "run_doctor",
        lambda **kwargs: calls.append(kwargs) or _doctor_report(),
    )
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path)

    assert report["mode"] == "check"
    assert _action_ids(report) == EXPECTED_ACTIONS
    assert _all_actions_ok(report)
    assert calls and _all_dry_run(calls)


def test_dependency_action_checks_lock_and_baseline_environment(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["mcp>=1.29,<2"]\n'
        '[project.optional-dependencies]\nmcp-server = []\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "mcp"\nversion = "1.29.0"\n',
        encoding="utf-8",
    )
    commands = []

    def run_uv(command, **kwargs):
        commands.append((command, kwargs))
        stdout = json.dumps({"sync": {"changes": []}}) if "--dry-run" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = sync_memory._dependency_action(root=tmp_path, apply=False, run_uv=run_uv)

    assert result["status"] == "ok"
    assert result["details"] == {"lock": "current", "environment": "current"}
    assert commands[0][0][:3] == ["uv", "lock", "--check"]
    assert commands[0][1]["timeout"] <= sync_memory.DEPENDENCY_TIMEOUT_SECONDS


def test_production_baseline_declares_python310_tomli_parser():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'tomli>=2.4.1,<3; python_version < \'3.11\'' in project


def test_importable_wrong_mcp_version_is_planned_and_repaired(tmp_path, monkeypatch):
    import importlib.util

    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    commands = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    def run_uv(command, **kwargs):
        commands.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"sync": {"changes": [{"package": "mcp", "version": "wrong"}]}}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    result = sync_memory._dependency_action(root=tmp_path, apply=True, run_uv=run_uv)

    assert result["status"] == "changed"
    assert result["details"] == {"lock": "current", "environment": "current"}
    assert len(commands) == 3
    assert "--dry-run" in commands[1]
    assert "--dry-run" not in commands[2]


def test_current_locked_environment_is_noop_even_without_import_probe(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    commands = []
    def run_uv(command, **kwargs):
        commands.append(command)
        stdout = json.dumps({"sync": {"changes": []}}) if "--dry-run" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = sync_memory._dependency_action(root=tmp_path, apply=True, run_uv=run_uv)

    assert result["status"] == "ok"
    assert result["details"] == {"lock": "current", "environment": "current"}
    assert len(commands) == 2
    assert "--dry-run" in commands[1]


def test_check_reports_planned_dependency_changes_without_applying(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    commands = []

    def run_uv(command, **kwargs):
        commands.append(command)
        stdout = (
            json.dumps({"sync": {"changes": [{"package": "mcp"}]}})
            if "--dry-run" in command
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = sync_memory._dependency_action(root=tmp_path, apply=False, run_uv=run_uv)

    assert result["status"] == "skipped"
    assert result["details"] == {"lock": "current", "environment": "stale"}
    assert len(commands) == 2
    assert all("--dry-run" not in command or "--locked" in command for command in commands)


@pytest.mark.parametrize("missing", ["pyproject.toml", "uv.lock"])
def test_dependency_action_rejects_missing_lock_inputs(tmp_path, missing):
    sync_memory = _load_sync_memory()
    for name in ("pyproject.toml", "uv.lock"):
        if name != missing:
            (tmp_path / name).write_text("version = 1\n", encoding="utf-8")

    result = sync_memory._dependency_action(
        root=tmp_path,
        apply=False,
        run_uv=lambda *args, **kwargs: pytest.fail("uv ran without lock inputs"),
    )

    assert result["status"] == "error"
    assert result["details"]["lock"] == "missing"


def test_apply_syncs_only_locked_production_baseline_without_models(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    commands = []
    def run_uv(command, **kwargs):
        commands.append(command)
        stdout = (
            json.dumps({"sync": {"changes": [{"package": "mcp"}]}})
            if "--dry-run" in command
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    result = sync_memory._dependency_action(root=tmp_path, apply=True, run_uv=run_uv)

    assert result["status"] == "changed"
    assert commands == [
        ["uv", "lock", "--check", "--no-python-downloads"],
        [
            "uv",
            "sync",
            "--locked",
            "--inexact",
            "--no-default-groups",
            "--dry-run",
            "--output-format",
            "json",
            "--no-python-downloads",
        ],
        [
            "uv",
            "sync",
            "--locked",
            "--inexact",
            "--no-default-groups",
            "--no-python-downloads",
            "--quiet",
        ]
    ]
    assert not any(
        heavy in commands[2] for heavy in ("full", "hybrid", "semantic", "reranker", "code-graph")
    )


def test_dependency_commands_share_one_deadline(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    timeouts = []
    def run_uv(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        time.sleep(0.03)
        stdout = (
            json.dumps({"sync": {"changes": [{"package": "mcp"}]}})
            if "--dry-run" in command
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    # The budget only has to outlast three 0.03 s commands plus process
    # overhead; 0.2 s left no room on a loaded macOS runner and the action
    # reported "error" for a machine-speed reason, not a contract one.
    result = sync_memory._dependency_action(
        root=tmp_path,
        apply=True,
        run_uv=run_uv,
        deadline=time.monotonic() + 2.0,
    )

    assert result["status"] == "changed"
    assert len(timeouts) == 3
    assert timeouts[1] < timeouts[0] - 0.02
    assert timeouts[2] < timeouts[1] - 0.02


def test_uv_commands_use_process_tree_runner(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []
    monkeypatch.setattr(
        sync_memory,
        "_run_process_tree",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, "", ""),
    )

    result = sync_memory._run_uv(["uv", "lock", "--check"], root=tmp_path, timeout=1)

    assert result.returncode == 0
    assert calls[0][0] == ["uv", "lock", "--check"]
    assert calls[0][1]["timeout"] == 1
    assert calls[0][1]["capture_output"] is True


def test_apply_rejects_stale_lock_even_when_mcp_is_installed(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    commands = []
    def run_uv(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "stale lock")

    result = sync_memory._dependency_action(root=tmp_path, apply=True, run_uv=run_uv)

    assert result["status"] == "error"
    assert result["details"]["lock"] == "stale"
    assert commands == [["uv", "lock", "--check", "--no-python-downloads"]]


def test_integration_config_state_is_reported_independently(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    report = _doctor_report(integrations="degraded")
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    result = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path)
    integration = next(action for action in result["actions"] if action["id"] == "integrations")

    assert integration["status"] == "skipped"
    assert "integration" in integration["message"].casefold()


def test_transaction_queue_and_index_freshness_states_are_independent(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    monkeypatch.setattr(
        sync_memory.doctor,
        "run_doctor",
        lambda **kwargs: _doctor_report(transactions="error", queue="degraded", index="degraded"),
    )
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    result = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path)
    actions = {action["id"]: action for action in result["actions"]}

    assert actions["transactions"]["status"] == "error"
    assert actions["queue"]["status"] == "skipped"
    assert actions["indexes"]["status"] == "skipped"
    assert actions["indexes"]["details"]["freshness"] == "missing"


def test_apply_leaves_prepared_transaction_and_flush_compile_queue_untouched(
    tmp_path, monkeypatch
):
    from markdown_transaction import MarkdownChange, MarkdownCoordinator
    from memory_queue import MemoryQueue

    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    target = root / "knowledge" / "notes" / "example.md"
    before = target.read_bytes()
    coordinator = MarkdownCoordinator(root, state)
    transaction = coordinator.prepare(
        [
            MarkdownChange.replace(
                "knowledge/notes/example.md",
                b"---\ntype: concept\n---\n# Mutated\n",
            )
        ],
        operation_id="sync-must-not-recover",
    )
    queue = MemoryQueue(state)
    flush_id = queue.enqueue("flush", 1, {"prompt": "write a daily entry"})
    compile_id = queue.enqueue("compile", 1, {})
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(
        root=root,
        state_root=state,
        home=home,
        apply=True,
        time_limit_seconds=5,
    )

    with sqlite3.connect(state / "run" / "markdown-transactions.sqlite3") as database:
        transaction_state = database.execute(
            'SELECT state FROM "transaction" WHERE id=?', (transaction.id,)
        ).fetchone()[0]
    assert target.read_bytes() == before
    assert transaction_state == "prepared"
    assert queue.get(flush_id).state == "ready"
    assert queue.get(compile_id).state == "ready"
    actions = {action["id"]: action for action in report["actions"]}
    assert actions["transactions"]["status"] in {"skipped", "error"}
    assert actions["queue"]["status"] in {"skipped", "error"}


def test_apply_repairs_only_runtime_and_keeps_diagnostics_idempotent(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    repaired_once = {"runtime"}
    calls = []

    def run_doctor(**kwargs):
        calls.append(kwargs)
        requested = set(kwargs.get("repair_actions") or ())
        repaired = []
        if kwargs["repair"] and requested & repaired_once:
            repaired = [{"action": f"repair_{next(iter(requested))}"}]
            repaired_once.difference_update(requested)
        return _doctor_report(repaired=repaired)

    monkeypatch.setattr(sync_memory.doctor, "run_doctor", run_doctor)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    first = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)
    second = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    first_states = {item["id"]: item["status"] for item in first["actions"]}
    assert first_states["environment"] == "changed"
    assert first_states["transactions"] == "ok"
    assert first_states["queue"] == "ok"
    assert first_states["indexes"] == "ok"
    assert all(item["status"] != "changed" for item in second["actions"])
    requested = [call.get("repair_actions") for call in calls if call["repair"]]
    assert requested == [{"runtime"}, {"runtime"}]


def test_blocking_index_builder_is_killed_before_timeout_is_reported(
    tmp_path, monkeypatch
):
    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    marker = tmp_path / "late-writer"
    builder = tmp_path / "blocking_index_builder.py"
    builder.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "time.sleep(0.6)\n"
        "Path(os.environ['SYNC_TEST_MARKER']).write_text('alive')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_memory, "INDEX_BUILDER_SCRIPT", builder)
    monkeypatch.setenv("SYNC_TEST_MARKER", str(marker))
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())
    monkeypatch.setattr(
        sync_memory.doctor,
        "run_doctor",
        lambda **kwargs: _doctor_report(index="degraded"),
    )

    started = time.monotonic()
    report = sync_memory.run_sync(
        root=root,
        state_root=state,
        home=home,
        apply=True,
        time_limit_seconds=0.2,
    )
    elapsed = time.monotonic() - started
    index = next(action for action in report["actions"] if action["id"] == "indexes")

    assert elapsed < 0.8
    assert index["status"] == "error"
    assert index["details"]["timed_out"] is True
    assert not marker.exists()
    time.sleep(0.7)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree behavior")
def test_timed_out_index_builder_kills_descendant_process(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    marker = tmp_path / "descendant-writer"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "time.sleep(0.8)\nPath(os.environ['SYNC_TEST_MARKER']).write_text('alive')\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync_memory, "INDEX_BUILDER_SCRIPT", parent)
    monkeypatch.setenv("SYNC_TEST_MARKER", str(marker))

    result = sync_memory._run_index_builder(
        root=tmp_path,
        state_root=tmp_path,
        timeout=0.2,
    )

    assert result["details"]["timed_out"] is True
    time.sleep(1)
    assert not marker.exists()


def test_process_runner_uses_posix_session_and_killpg(monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []

    class Process:
        pid = 42
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len([item for item in calls if item[0] == "communicate"]) == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            self.returncode = -9
            return "", ""

    monkeypatch.setattr(
        sync_memory,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda pid: 42,
            killpg=lambda pgid, signal: calls.append(("killpg", pgid)),
        ),
    )
    monkeypatch.setattr(
        sync_memory.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append(("popen", kwargs)) or Process(),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        sync_memory._run_process_tree(["cmd"], timeout=0.1)

    assert next(item for item in calls if item[0] == "popen")[1]["start_new_session"] is True
    assert ("killpg", 42) in calls
    assert calls[-1] == ("communicate", sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS)


def test_process_runner_uses_windows_group_and_tree_termination(monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []

    class Process:
        pid = 42
        returncode = None
        attempts = 0

        def communicate(self, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            self.returncode = 1
            return "", ""

    monkeypatch.setattr(sync_memory, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        sync_memory.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append(kwargs) or Process(),
    )
    monkeypatch.setattr(
        sync_memory,
        "_terminate_windows_tree",
        lambda process: calls.append(("terminate-tree", process.pid)),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        sync_memory._run_process_tree(["cmd"], timeout=0.1)

    assert calls[0]["creationflags"] & sync_memory.WINDOWS_NEW_PROCESS_GROUP
    assert ("terminate-tree", 42) in calls


def test_windows_failed_taskkill_kills_direct_process_and_reports_cleanup_failure(monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []

    class Pipe:
        def close(self):
            calls.append("close")

    class Process:
        pid = 42
        returncode = None
        stdout = Pipe()
        stderr = Pipe()

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len([item for item in calls if isinstance(item, tuple)]) <= 1:
                raise subprocess.TimeoutExpired(["uv"], timeout)
            self.returncode = 1
            return "", ""

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(sync_memory, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sync_memory.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(sync_memory, "_terminate_windows_tree", lambda process: "taskkill_failed")

    with pytest.raises(sync_memory.ProcessTreeTimeout) as error:
        sync_memory._run_process_tree(["uv"], timeout=0.1)

    assert error.value.cleanup_error == "taskkill_failed"
    assert "kill" in calls
    assert calls[-1][0] == "communicate"
    assert calls[-1][1] <= sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS


def test_windows_taskkill_nonzero_return_is_bounded_failure(monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []

    class Terminator:
        returncode = 1

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            return "", ""

    monkeypatch.setattr(sync_memory.subprocess, "Popen", lambda *args, **kwargs: Terminator())
    monkeypatch.setattr(
        sync_memory,
        "os",
        SimpleNamespace(name="nt", environ={"SystemRoot": r"C:\Windows"}),
    )

    result = sync_memory._terminate_windows_tree(SimpleNamespace(pid=42))

    assert result == "taskkill_failed"
    assert calls == [("communicate", sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS)]


def test_retained_descendant_pipes_are_closed_without_unbounded_communicate(monkeypatch):
    sync_memory = _load_sync_memory()
    calls = []

    class Pipe:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(("close", self.name))

    class Process:
        pid = 42
        returncode = None
        stdout = Pipe("stdout")
        stderr = Pipe("stderr")

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            raise subprocess.TimeoutExpired(["uv"], timeout)

        def kill(self):
            calls.append(("kill",))

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            self.returncode = 1
            return 1

    monkeypatch.setattr(sync_memory, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sync_memory.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(sync_memory, "_terminate_windows_tree", lambda process: "taskkill_failed")

    with pytest.raises(sync_memory.ProcessTreeTimeout) as error:
        sync_memory._run_process_tree(["uv"], timeout=0.1, capture_output=True)

    assert error.value.cleanup_error == "taskkill_failed;retained_pipes"
    assert ("close", "stdout") in calls
    assert ("close", "stderr") in calls
    assert ("wait", sync_memory.PROCESS_CLEANUP_TIMEOUT_SECONDS) in calls
    assert all(item != ("communicate", None) for item in calls)


def test_dependency_timeout_exposes_cleanup_failure(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")
    def timed_out(*args, **kwargs):
        raise sync_memory.ProcessTreeTimeout(["uv"], 1, cleanup_error="taskkill_failed")

    result = sync_memory._dependency_action(root=tmp_path, apply=False, run_uv=timed_out)

    assert result["status"] == "error"
    assert result["details"]["cleanup_error"] == "taskkill_failed"


def test_dependency_action_reports_expired_deadline_without_starting_uv(tmp_path):
    sync_memory = _load_sync_memory()
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["mcp>=1.29,<2"]\nmcp-server = []\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text('name = "mcp"\nmcp-server\n', encoding="utf-8")

    result = sync_memory._dependency_action(
        root=tmp_path,
        apply=False,
        run_uv=lambda *args, **kwargs: pytest.fail("uv started after deadline"),
        deadline=time.monotonic() - 1,
    )

    assert result["status"] == "error"
    assert result["details"]["timed_out"] is True


def test_sync_index_builder_excludes_symlinked_outside_page(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    outside = tmp_path / "outside-secret.md"
    secret = "OUTSIDE-SYMLINK-SECRET"
    outside.write_text(f"# Outside\n{secret}\n", encoding="utf-8")
    link = root / "knowledge" / "notes" / "linked.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(
        root=root,
        state_root=state,
        home=home,
        apply=True,
        time_limit_seconds=5,
    )

    assert next(item for item in report["actions"] if item["id"] == "indexes")["status"] == "changed"
    with sqlite3.connect(state / "cache" / "index.sqlite") as database:
        rows = database.execute("SELECT path, body FROM pages").fetchall()
    assert secret not in json.dumps(rows)
    assert "knowledge/notes/linked.md" not in json.dumps(rows)


@pytest.mark.parametrize("apply", [False, True], ids=["check", "apply"])
@pytest.mark.parametrize("change", ["added", "removed", "modified"])
def test_sync_detects_recent_knowledge_source_changes(
    tmp_path, monkeypatch, apply, change
):
    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    _build_fresh_search_index(sync_memory, root, state)
    index = state / "cache" / "index.sqlite"
    index_mtime = time.time() - 60
    os.utime(index, (index_mtime, index_mtime))
    original = root / "knowledge" / "notes" / "example.md"
    _apply_knowledge_change(root, original, change)
    knowledge = root / "knowledge"
    before = _snapshot(knowledge)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(
        root=root,
        state_root=state,
        home=home,
        apply=apply,
        time_limit_seconds=5,
    )

    _assert_stale_index_action(report, apply)
    assert _snapshot(knowledge) == before


@pytest.mark.parametrize("apply", [False, True], ids=["check", "apply"])
@pytest.mark.parametrize("manifest_state", ["missing", "invalid"])
def test_sync_treats_missing_or_invalid_source_manifest_as_stale(
    tmp_path, monkeypatch, apply, manifest_state
):
    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    _build_fresh_search_index(sync_memory, root, state)
    manifest = state / "cache" / ".paths-manifest"
    _damage_manifest(manifest, manifest_state)
    knowledge = root / "knowledge"
    before = _snapshot(knowledge)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(
        root=root,
        state_root=state,
        home=home,
        apply=apply,
        time_limit_seconds=5,
    )

    _assert_stale_index_action(report, apply)
    assert _snapshot(knowledge) == before


def test_apply_is_bounded_by_explicit_action_limit(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    doctor_calls = []
    monkeypatch.setattr(
        sync_memory.doctor,
        "run_doctor",
        lambda **kwargs: doctor_calls.append(kwargs) or _doctor_report(),
    )
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    report = sync_memory.run_sync(
        root=tmp_path,
        state_root=tmp_path,
        home=tmp_path,
        apply=True,
        action_limit=3,
        time_limit_seconds=1,
    )

    assert report["limits"] == {"actions": 3, "seconds": 1.0}
    assert [item["status"] for item in report["actions"][3:]] == ["skipped"] * 4
    assert all(call.get("repair_actions") in (None, {"runtime"}) for call in doctor_calls)


def test_apply_never_writes_under_read_only_knowledge_tree(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    root, state, home = _build_vault(tmp_path)
    _create_index(state / "cache" / "index.sqlite")
    knowledge = root / "knowledge"
    before = _snapshot(knowledge)
    _set_tree_mode(knowledge, read_only=True)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())

    try:
        report = sync_memory.run_sync(
            root=root,
            state_root=state,
            home=home,
            apply=True,
            time_limit_seconds=5,
        )
    finally:
        _set_tree_mode(knowledge, read_only=False)

    assert _snapshot(knowledge) == before
    assert _action_ids(report) == EXPECTED_ACTIONS


def test_cli_json_defaults_to_check_and_rejects_conflicting_flags(monkeypatch, capsys):
    sync_memory = _load_sync_memory()
    monkeypatch.setattr(
        sync_memory,
        "run_sync",
        lambda **kwargs: {
            "mode": "apply" if kwargs["apply"] else "check",
            "overall_status": "ok",
            "actions": [],
        },
    )

    assert sync_memory.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "check"
    with pytest.raises(SystemExit) as error:
        sync_memory.main(["--check", "--apply"])
    assert error.value.code == 2


def test_cli_json_emits_report_and_returns_nonzero_for_degraded(monkeypatch, capsys):
    sync_memory = _load_sync_memory()
    monkeypatch.setattr(
        sync_memory,
        "run_sync",
        lambda **kwargs: {"mode": "check", "overall_status": "degraded", "actions": []},
    )

    assert sync_memory.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["overall_status"] == "degraded"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--time-limit-seconds", "nan"],
        ["--time-limit-seconds", "inf"],
        ["--time-limit-seconds", "0"],
        ["--time-limit-seconds", "-1"],
        ["--action-limit", "0"],
        ["--action-limit", "8"],
    ],
)
def test_cli_rejects_invalid_limits_without_traceback(arguments, capsys):
    sync_memory = _load_sync_memory()

    with pytest.raises(SystemExit) as error:
        sync_memory.main(arguments)

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "Traceback" not in captured.err


def test_installers_handle_sync_exit_codes_explicitly():
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert 'sync_memory.py" --apply' in powershell
    shell_line = _sync_apply_line(shell)
    powershell_line = _sync_apply_line(powershell)
    assert "|| true" not in shell_line
    assert "2>$null" not in powershell_line
    assert "Out-Null" not in powershell_line
    assert 'uv run --locked --no-sync python "$VAULT_ROOT/scripts/sync_memory.py" --apply' in shell
    assert 'uv run --locked --no-sync python "$VAULT_ROOT\\scripts\\sync_memory.py" --apply' in powershell
    assert 'case "$SYNC_EXIT" in' in shell
    assert 'warn "Runtime synchronization completed with warnings"' in shell
    assert '*) fail "Runtime synchronization failed"' in shell
    assert "switch ($syncExit)" in powershell
    assert 'Warn "Runtime synchronization completed with warnings"' in powershell
    assert 'default { Fail "Runtime synchronization failed" }' in powershell
    assert 'LLM-Wiki installed with warnings' in shell
    assert 'LLM-Wiki installed with warnings' in powershell


def test_powershell_installer_scheduler_failure_is_partial_and_nonzero(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    scheduler = source.split(
        "# --- 6. Register Task Scheduler -----------------------------------", 1
    )[1].split("# --- 7. Detect and wire up agents ---------------------------------", 1)[0]
    summary = source.split(
        "# --- 9. Summary ---------------------------------------------------", 1
    )[1]
    harness = f"""
function Info([string]$Message) {{ Write-Output "INFO:$Message" }}
function Warn([string]$Message) {{ Write-Output "WARN:$Message" }}
function Ok([string]$Message) {{ Write-Output "OK:$Message" }}
function uv {{ }}
function Invoke-NativeCommand {{ throw 'injected install-control failure' }}
$VAULT_ROOT = 'C:\\vault'
$STATE_ROOT = 'C:\\state'
$agents = @()
$syncWarning = $false
{scheduler}
{summary}
"""

    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", harness],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "LLM-Wiki installed with warnings" in completed.stdout
    assert "Maintenance: not registered" in completed.stdout
    assert "LLM-Wiki installed successfully" not in completed.stdout
    assert "Maintenance: Task Scheduler (nightly + weekly)" not in completed.stdout


def test_powershell_installer_verifies_registered_scheduler_state(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    scheduler = source.split(
        "# --- 6. Register Task Scheduler -----------------------------------", 1
    )[1].split("# --- 7. Detect and wire up agents ---------------------------------", 1)[0]
    summary = source.split(
        "# --- 9. Summary ---------------------------------------------------", 1
    )[1]
    harness = f"""
function Info([string]$Message) {{ Write-Output "INFO:$Message" }}
function Warn([string]$Message) {{ Write-Output "WARN:$Message" }}
function Ok([string]$Message) {{ Write-Output "OK:$Message" }}
function uv {{ }}
function Invoke-NativeCommand {{ '{{"status":"committed","scheduler_backend":"invalid"}}' }}
$VAULT_ROOT = 'C:\\vault'
$STATE_ROOT = 'C:\\state'
$agents = @()
$syncWarning = $false
{scheduler}
{summary}
"""

    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", harness],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 1
    assert "Install ownership transaction or Task Scheduler verification failed" in completed.stdout
    assert "Maintenance: not registered" in completed.stdout
    assert "LLM-Wiki installed successfully" not in completed.stdout


def test_sync_reports_legacy_change_separately_when_generation_is_deferred(
    tmp_path, monkeypatch
):
    sync_memory = _load_sync_memory()
    report = _doctor_report(index="degraded", overall="degraded")
    report["checks"].append(
        {
            "id": "generation",
            "status": "degraded",
            "message": "generation is stale",
            "details": {"freshness": "stale", "repairable": True},
        }
    )
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: _dependency_result())
    monkeypatch.setattr(
        sync_memory,
        "_run_generation_builder",
        lambda **kwargs: sync_memory._result(
            "indexes",
            "skipped",
            "Evidence generation refresh was deferred; retry will rebuild from source.",
            {"generation": "candidate", "partial": True, "reason": "time_limit"},
        ),
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_index_builder",
        lambda **kwargs: sync_memory._result(
            "indexes", "changed", "Derived search index was rebuilt.", {}
        ),
    )

    result = sync_memory.run_sync(
        root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True
    )
    indexes = next(action for action in result["actions"] if action["id"] == "indexes")

    assert indexes["status"] == "skipped"
    assert result["overall_status"] == "degraded"
    assert indexes["details"]["legacy_index"] == "changed"
    assert indexes["details"]["generation_refresh"] == "skipped"
    assert "synchronized" not in indexes["message"].casefold()


def test_generation_timeout_does_not_claim_nonexistent_continuation(tmp_path, monkeypatch):
    sync_memory = _load_sync_memory()
    monkeypatch.setattr(
        sync_memory.doctor,
        "run_generation_maintenance",
        lambda **kwargs: {
            "status": "deferred",
            "generation_id": None,
            "partial": True,
            "reason": "time_limit",
        },
    )

    result = sync_memory._run_generation_builder(
        root=tmp_path,
        state_root=tmp_path,
        timeout=30,
        max_sources=10,
    )

    assert result["status"] == "skipped"
    assert "continuation" not in result["message"].casefold()
    assert "retry" in result["message"].casefold()


@pytest.mark.parametrize(
    ("sync_exit", "expected_exit", "expected_text", "forbidden_text"),
    [
        (0, 0, "OK:Runtime state synchronized", "WARN:"),
        (1, 0, "WARN:Runtime synchronization completed with warnings", "OK:Runtime state synchronized"),
        (2, 2, "FAIL:Runtime synchronization failed", "OK:Runtime state synchronized"),
    ],
)
def test_powershell_installer_sync_block_behaves_by_exit_code(
    sync_exit, expected_exit, expected_text, forbidden_text
):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    source = (ROOT / "install.ps1").read_text(encoding="utf-8")
    block = source.split(
        "# --- 8. Bounded runtime sync --------------------------------------", 1
    )[1]
    harness = f"""
function Info([string]$Message) {{ Write-Output "INFO:$Message" }}
function Warn([string]$Message) {{ Write-Output "WARN:$Message" }}
function Ok([string]$Message) {{ Write-Output "OK:$Message" }}
function Fail([string]$Message) {{ Write-Output "FAIL:$Message"; exit 2 }}
function uv {{ $global:LASTEXITCODE = {sync_exit} }}
$VAULT_ROOT = 'C:\\vault'
$agents = @()
{block}
"""

    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", harness],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )

    assert completed.returncode == expected_exit
    assert expected_text in completed.stdout
    assert forbidden_text not in completed.stdout


def test_sync_has_no_git_or_knowledge_mutation_code():
    source = SYNC_SCRIPT.read_text(encoding="utf-8")

    assert "git " not in source
    assert "knowledge/" not in source
    assert "knowledge\\" not in source
    assert "write_text(" not in source
    assert "write_bytes(" not in source
    assert "[project.scripts]" not in source
