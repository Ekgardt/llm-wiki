from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import install_control
import pytest
from install_control import (
    ManagedResource,
    _parser,
    cron_scheduler_resource,
    file_resource,
    inspect_install_state,
    install_resources,
    launchd_scheduler_resource,
    main,
    profile_resource,
    render_launchd_definitions,
    render_systemd_definitions,
    rollback_resources,
    select_scheduler_backend,
    systemd_scheduler_resource,
    uninstall_resources,
    validate_install_state,
    windows_environment_resources,
    windows_task_scheduler_resource,
)


def test_status_is_read_only_when_install_state_is_absent(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()

    assert inspect_install_state(state_root) == {
        "manifest": "absent",
        "status": "absent",
        "transaction": "absent",
    }
    assert not (state_root / "run" / "install").exists()


def _release() -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": "4.0.0",
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


class _FakeSystemd:
    def __init__(self) -> None:
        self.enabled: set[str] = set()
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        self.calls.append(command)
        handlers = {
            "disable": self._disable,
            "enable": self._enable,
            "is-active": self._is_active,
            "is-enabled": self._is_enabled,
        }
        return handlers.get(command[2], self._ok)(command)

    def _is_enabled(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[3] in self.enabled:
            return 0, b"enabled\n"
        return 1, b"disabled\n"

    def _is_active(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[3] in self.enabled:
            return 0, b"active\n"
        return 3, b"inactive\n"

    def _enable(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.enabled.update(command[4:])
        return 0, b""

    def _disable(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.enabled.difference_update(command[4:])
        return 0, b""

    def _ok(self, _command: tuple[str, ...]) -> tuple[int, bytes]:
        return 0, b""


class _FakeLaunchd:
    def __init__(self) -> None:
        self.active: set[str] = set()

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        handlers = {
            "bootstrap": self._bootstrap,
            "bootout": self._bootout,
            "print": self._print,
        }
        return handlers[command[1]](command)

    def _bootstrap(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.active.add(Path(command[3]).stem)
        return 0, b""

    def _bootout(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        self.active.discard(command[2].rsplit("/", 1)[-1])
        return 0, b""

    def _print(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if command[2].rsplit("/", 1)[-1] in self.active:
            return 0, b"active\n"
        return 113, b"not found\n"


class _FakeCron:
    def __init__(self, table: bytes | None) -> None:
        self.table = table

    def __call__(
        self, command: tuple[str, ...], input_value: bytes | None = None
    ) -> tuple[int, bytes]:
        handlers = {
            "-": self._replace,
            "-l": self._list,
            "-r": self._remove,
        }
        return handlers[command[1]](input_value)

    def _list(self, _input: bytes | None) -> tuple[int, bytes]:
        if self.table is None:
            return 1, b""
        return 0, self.table

    def _replace(self, input_value: bytes | None) -> tuple[int, bytes]:
        assert input_value is not None
        self.table = input_value
        return 0, b""

    def _remove(self, _input: bytes | None) -> tuple[int, bytes]:
        self.table = None
        return 0, b""


class _FakeWindowsTasks:
    def __init__(self) -> None:
        self.state = "absent"

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        if "-StateJson" in command:
            return 0, json.dumps({"state": self.state}).encode()
        if "-Uninstall" in command:
            self.state = "absent"
            return 0, b""
        self.state = "equivalent"
        return 0, b""


class _SpecAwareWindowsTasks:
    def __init__(self) -> None:
        self.installed: bytes | None = None

    def __call__(self, command: tuple[str, ...], _input: bytes | None = None) -> tuple[int, bytes]:
        candidate = self._candidate(command)
        if "-StateJson" in command:
            return 0, json.dumps({"state": self._state(candidate)}).encode()
        if "-Uninstall" in command:
            return self._uninstall(candidate)
        return self._install(candidate)

    def _candidate(self, command: tuple[str, ...]) -> bytes:
        return install_control.render_windows_task_spec(
            Path(command[command.index("-VaultRoot") + 1]),
            Path(command[command.index("-StateRoot") + 1]),
            Path(command[command.index("-UvPath") + 1]),
        )

    def _state(self, candidate: bytes) -> str:
        if self.installed is None:
            return "absent"
        if self.installed == candidate:
            return "equivalent"
        return "conflict"

    def _uninstall(self, candidate: bytes) -> tuple[int, bytes]:
        if self.installed != candidate:
            return 1, b""
        self.installed = None
        return 0, b""

    def _install(self, candidate: bytes) -> tuple[int, bytes]:
        if self.installed is not None and self.installed != candidate:
            return 1, b""
        self.installed = candidate
        return 0, b""


class _InterruptingValue:
    def __init__(self, crash_point: str) -> None:
        self.crash_point = crash_point
        self.enabled = False
        self.value: bytes | None = None

    def read(self) -> bytes | None:
        return self.value

    def write(self, updated: bytes | None) -> None:
        self.value = updated
        if self.enabled and self.crash_point == "after_mutation":
            raise KeyboardInterrupt("simulated process interruption")


def _legacy_definition_resource(resource: ManagedResource) -> ManagedResource:
    definitions = {
        relative.rsplit("/", 1)[-1]: value for relative, value in resource.definitions.items()
    }
    legacy = install_control._definition_digest(definitions)

    def read_owned() -> bytes | None:
        current = resource.read_owned()
        if current == resource.desired:
            return legacy
        return current

    return ManagedResource(
        resource_id=resource.resource_id,
        kind=resource.kind,
        locator=resource.locator,
        desired=legacy,
        read_owned=read_owned,
        write_owned=resource.write_owned,
        recognizes=lambda current: current == legacy,
        metadata=resource.metadata,
        definitions=resource.definitions,
        adopt_as_absent=resource.adopt_as_absent,
    )


def _definition_directory_state(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def _systemd_v2_scenario(tmp_path: Path) -> dict[str, object]:
    runner = _FakeSystemd()
    state_root = tmp_path / "state"
    unit_dir = tmp_path / "systemd"
    vault = tmp_path / "vault"
    old_uv = tmp_path / "old" / "uv"
    new_uv = tmp_path / "new" / "uv"
    old = systemd_scheduler_resource(
        root=vault,
        state_root=state_root,
        uv_path=old_uv,
        unit_directory=unit_dir,
        runner=runner,
        systemctl="systemctl",
    )
    new = systemd_scheduler_resource(
        root=vault,
        state_root=state_root,
        uv_path=new_uv,
        unit_directory=unit_dir,
        runner=runner,
        systemctl="systemctl",
    )
    return {
        "backend": "systemd_user",
        "current": lambda: _definition_directory_state(unit_dir),
        "new": new,
        "new_state": render_systemd_definitions(vault, state_root, new_uv),
        "old": _legacy_definition_resource(old),
        "old_state": render_systemd_definitions(vault, state_root, old_uv),
        "state_root": state_root,
        "vault": vault,
    }


def _launchd_v2_scenario(tmp_path: Path) -> dict[str, object]:
    runner = _FakeLaunchd()
    state_root = tmp_path / "state"
    agents = tmp_path / "LaunchAgents"
    vault = tmp_path / "vault"
    old_uv = tmp_path / "old" / "uv"
    new_uv = tmp_path / "new" / "uv"
    old = launchd_scheduler_resource(
        root=vault,
        state_root=state_root,
        uv_path=old_uv,
        launch_agents_directory=agents,
        uid=501,
        runner=runner,
        launchctl="launchctl",
    )
    new = launchd_scheduler_resource(
        root=vault,
        state_root=state_root,
        uv_path=new_uv,
        launch_agents_directory=agents,
        uid=501,
        runner=runner,
        launchctl="launchctl",
    )
    return {
        "backend": "launchd",
        "current": lambda: _definition_directory_state(agents),
        "new": new,
        "new_state": render_launchd_definitions(vault, state_root, new_uv),
        "old": _legacy_definition_resource(old),
        "old_state": render_launchd_definitions(vault, state_root, old_uv),
        "state_root": state_root,
        "vault": vault,
    }


def _windows_tasks_v2_scenario(tmp_path: Path) -> dict[str, object]:
    runner = _SpecAwareWindowsTasks()
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    old_uv = tmp_path / "old" / "uv.exe"
    new_uv = tmp_path / "new" / "uv.exe"
    arguments = {
        "root": vault,
        "state_root": state_root,
        "script_path": tmp_path / "install-scheduled-tasks.ps1",
        "powershell": "pwsh.exe",
        "runner": runner,
    }
    old = windows_task_scheduler_resource(uv_path=old_uv, **arguments)
    new = windows_task_scheduler_resource(uv_path=new_uv, **arguments)
    return {
        "backend": "task_scheduler",
        "current": lambda: runner.installed,
        "new": new,
        "new_state": install_control.render_windows_task_spec(vault, state_root, new_uv),
        "old": old,
        "old_state": install_control.render_windows_task_spec(vault, state_root, old_uv),
        "state_root": state_root,
        "vault": vault,
    }


def test_transaction_is_durable_before_first_external_mutation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    observed: list[dict[str, object]] = []
    value: bytes | None = None

    def read_owned() -> bytes | None:
        return value

    def write_owned(updated: bytes | None) -> None:
        nonlocal value
        transaction = json.loads(
            (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
        )
        observed.append(transaction)
        value = updated

    resource = ManagedResource(
        resource_id="test-resource",
        kind="test_value",
        locator="test://resource",
        desired=b"installed",
        read_owned=read_owned,
        write_owned=write_owned,
        recognizes=lambda current: current == b"installed",
    )

    manifest = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
    )

    assert value == b"installed"
    assert observed[0]["state"] == "mutating"
    assert observed[0]["resources"][0]["state"] == "mutating"
    assert manifest["schema"] == "install-manifest/v1"
    assert manifest["resources"][0]["installed"]["sha256"]
    transaction = json.loads(
        (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "committed"


def test_profile_install_preserves_unrelated_bytes_and_mode_without_copying_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    profile = tmp_path / ".profile"
    profile.write_bytes(b"export PRIVATE_TOKEN=do-not-copy\n")
    profile.chmod(0o640)
    original = profile.stat()
    original_mode = stat.S_IMODE(original.st_mode)
    owner_updates: list[tuple[int, int]] = []
    monkeypatch.setattr(
        install_control.os,
        "chown",
        lambda _path, uid, gid: owner_updates.append((uid, gid)),
        raising=False,
    )
    vault_root = tmp_path / "vault"

    manifest = install_resources(
        state_root=state_root,
        vault_root=vault_root,
        release=_release(),
        scheduler_backend="cron",
        resources=[profile_resource(profile, vault_root, state_root)],
    )

    installed = profile.read_bytes()
    assert installed.startswith(b"export PRIVATE_TOKEN=do-not-copy\n")
    assert installed.count(b"# >>> LLM-Wiki installer >>>") == 1
    assert stat.S_IMODE(profile.stat().st_mode) == original_mode
    assert owner_updates == [(original.st_uid, original.st_gid)]
    assert manifest["resources"][0]["origin"] == {"state": "absent"}
    preimages = (state_root / "run" / "install" / "preimages").glob("*.bin")
    assert all(b"PRIVATE_TOKEN" not in path.read_bytes() for path in preimages)


def test_failed_resource_rolls_back_prior_verified_mutations(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    values: dict[str, bytes | None] = {"first": None, "second": None}

    def resource(name: str, *, fail: bool = False) -> ManagedResource:
        def read_owned() -> bytes | None:
            return values[name]

        def write_owned(updated: bytes | None) -> None:
            values[name] = updated
            if fail and updated is not None:
                raise RuntimeError("injected failure")

        return ManagedResource(
            resource_id=name,
            kind="test_value",
            locator=f"test://{name}",
            desired=f"installed-{name}".encode(),
            read_owned=read_owned,
            write_owned=write_owned,
            recognizes=lambda current: current.startswith(b"installed-"),
        )

    with pytest.raises(RuntimeError, match="injected failure"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release=_release(),
            scheduler_backend="cron",
            resources=[resource("first"), resource("second", fail=True)],
        )

    assert values == {"first": None, "second": None}
    transaction = json.loads(
        (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "reverted"
    assert not (state_root / "run" / "install" / "manifest.json").exists()


def test_identical_rerun_preserves_original_manifest_and_preimage_baseline(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def read_owned() -> bytes | None:
        return value

    def write_owned(updated: bytes | None) -> None:
        nonlocal value
        value = updated

    resource = ManagedResource(
        resource_id="stable",
        kind="test_value",
        locator="test://stable",
        desired=b"installed",
        read_owned=read_owned,
        write_owned=write_owned,
        recognizes=lambda current: current == b"installed",
    )
    arguments = {
        "state_root": state_root,
        "vault_root": tmp_path / "vault",
        "release": _release(),
        "scheduler_backend": "cron",
        "resources": [resource],
    }

    first = install_resources(**arguments)
    manifest_path = state_root / "run" / "install" / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    second = install_resources(**arguments)

    assert second == first
    assert manifest_path.read_bytes() == manifest_bytes
    assert first["resources"][0]["origin"] == {"state": "absent"}
    assert not list((state_root / "run" / "install" / "preimages").glob("*.bin"))


def test_crash_after_external_write_resumes_original_transaction(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value_path = tmp_path / "owned-value"
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from install_control import ManagedResource, install_resources

        value_path = Path({str(value_path)!r})
        def read_owned():
            return value_path.read_bytes() if value_path.exists() else None
        def write_owned(value):
            value_path.write_bytes(value)
            os._exit(91)
        resource = ManagedResource(
            resource_id="crash-value",
            kind="test_value",
            locator="test://crash-value",
            desired=b"installed",
            read_owned=read_owned,
            write_owned=write_owned,
            recognizes=lambda current: current == b"installed",
        )
        install_resources(
            state_root=Path({str(state_root)!r}),
            vault_root=Path({str(tmp_path / "vault")!r}),
            release={_release()!r},
            scheduler_backend="cron",
            resources=[resource],
        )
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "scripts")
    crashed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        timeout=30,
        check=False,
    )
    transaction_path = state_root / "run" / "install" / "transaction.json"
    interrupted = json.loads(transaction_path.read_text(encoding="utf-8"))

    assert crashed.returncode == 91
    assert interrupted["state"] == "mutating"
    assert interrupted["resources"][0]["origin"] == {"state": "absent"}

    def read_owned() -> bytes | None:
        return value_path.read_bytes() if value_path.exists() else None

    def write_owned(value: bytes | None) -> None:
        if value is None:
            value_path.unlink(missing_ok=True)
            return
        value_path.write_bytes(value)

    resumed = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[
            ManagedResource(
                resource_id="crash-value",
                kind="test_value",
                locator="test://crash-value",
                desired=b"installed",
                read_owned=read_owned,
                write_owned=write_owned,
                recognizes=lambda current: current == b"installed",
            )
        ],
    )

    terminal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert terminal["id"] == interrupted["id"]
    assert terminal["state"] == "committed"
    assert resumed["resources"][0]["origin"] == {"state": "absent"}


def test_explicit_rollback_finishes_interrupted_install(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value_path = tmp_path / "rollback-value"
    script = textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from install_control import ManagedResource, install_resources
        value_path = Path({str(value_path)!r})
        def read_owned():
            return value_path.read_bytes() if value_path.exists() else None
        def write_owned(value):
            value_path.write_bytes(value)
            os._exit(92)
        install_resources(
            state_root=Path({str(state_root)!r}),
            vault_root=Path({str(tmp_path / "vault")!r}),
            release={_release()!r},
            scheduler_backend="cron",
            resources=[ManagedResource(
                resource_id="rollback-value", kind="test_value",
                locator="test://rollback-value", desired=b"installed",
                read_owned=read_owned, write_owned=write_owned,
                recognizes=lambda current: current == b"installed",
            )],
        )
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "scripts")
    crashed = subprocess.run(
        [sys.executable, "-c", script], env=environment, timeout=30, check=False
    )
    assert crashed.returncode == 92

    def read_owned() -> bytes | None:
        return value_path.read_bytes() if value_path.exists() else None

    def write_owned(value: bytes | None) -> None:
        if value is None:
            value_path.unlink(missing_ok=True)
            return
        value_path.write_bytes(value)

    result = rollback_resources(
        state_root=state_root,
        resources=[
            ManagedResource(
                resource_id="rollback-value",
                kind="test_value",
                locator="test://rollback-value",
                desired=b"installed",
                read_owned=read_owned,
                write_owned=write_owned,
                recognizes=lambda current: current == b"installed",
            )
        ],
    )

    assert result["state"] == "reverted"
    assert not value_path.exists()


@pytest.mark.parametrize(
    "original",
    [b"", b"export USER_VALUE=yes", b"export USER_VALUE=yes\n"],
)
def test_uninstall_restores_exact_profile_bytes_without_whole_file_preimage(
    tmp_path: Path, original: bytes
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    profile = tmp_path / ".profile"
    if original:
        profile.write_bytes(original)
    resource = profile_resource(profile, tmp_path / "vault", state_root)
    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
    )

    result = uninstall_resources(state_root=state_root, resources=[resource])

    if original:
        assert profile.read_bytes() == original
    else:
        assert not profile.exists()
    assert result["operation"] == "uninstall"
    assert result["state"] == "committed"
    assert not (state_root / "run" / "install" / "manifest.json").exists()


@pytest.mark.parametrize(
    ("platform", "available", "expected"),
    [
        ("win32", False, "task_scheduler"),
        ("darwin", False, "launchd"),
        ("linux", True, "systemd_user"),
    ],
)
def test_native_scheduler_selection_is_platform_specific(
    platform: str, available: bool, expected: str
) -> None:
    assert select_scheduler_backend(platform, "native", available) == expected


def test_linux_cron_fallback_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="systemd user manager"):
        select_scheduler_backend("linux", "native", False)
    assert select_scheduler_backend("linux", "cron", False) == "cron"
    with pytest.raises(ValueError, match="cron"):
        select_scheduler_backend("win32", "cron", False)


def test_launchd_definitions_use_exact_arguments_and_login_scoped_calendars(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault with space"
    state = tmp_path / "state with space"
    uv_path = tmp_path / "bin with space" / "uv"

    definitions = render_launchd_definitions(root, state, uv_path)

    assert set(definitions) == {
        "io.github.ekgardt.llm-wiki.nightly.plist",
        "io.github.ekgardt.llm-wiki.weekly.plist",
    }
    nightly = plistlib.loads(definitions["io.github.ekgardt.llm-wiki.nightly.plist"])
    assert nightly["ProgramArguments"] == [
        str(uv_path.resolve()),
        "run",
        "--locked",
        "--no-sync",
        "--directory",
        str(root.resolve()),
        "python",
        "scripts/scheduled_nightly.py",
    ]
    assert nightly["EnvironmentVariables"] == {
        "LLM_WIKI_ROOT": str(root.resolve()),
        "LLM_WIKI_STATE_ROOT": str(state.resolve()),
    }
    assert nightly["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
    weekly = plistlib.loads(definitions["io.github.ekgardt.llm-wiki.weekly.plist"])
    assert weekly["StartCalendarInterval"] == {
        "Hour": 4,
        "Minute": 0,
        "Weekday": 0,
    }


def test_systemd_definitions_are_persistent_user_timers_without_shell(tmp_path: Path) -> None:
    root = tmp_path / "vault with space"
    state = tmp_path / "state with space"
    uv_path = tmp_path / "bin with space" / "uv"

    definitions = render_systemd_definitions(root, state, uv_path)

    assert set(definitions) == {
        "llm-wiki-nightly.service",
        "llm-wiki-nightly.timer",
        "llm-wiki-weekly.service",
        "llm-wiki-weekly.timer",
    }
    nightly_service = definitions["llm-wiki-nightly.service"].decode()
    nightly_timer = definitions["llm-wiki-nightly.timer"].decode()
    assert "Type=oneshot" in nightly_service
    assert "scripts/scheduled_nightly.py" in nightly_service
    assert "sh -c" not in nightly_service
    escaped_root = str(root.resolve()).replace("\\", "\\\\")
    escaped_state = str(state.resolve()).replace("\\", "\\\\")
    assert f'Environment="LLM_WIKI_ROOT={escaped_root}"' in nightly_service
    assert f'Environment="LLM_WIKI_STATE_ROOT={escaped_state}"' in nightly_service
    assert "OnCalendar=*-*-* 03:00:00" in nightly_timer
    assert "Persistent=true" in nightly_timer
    assert "WantedBy=timers.target" in nightly_timer


def test_systemd_working_directory_is_an_unquoted_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "vault with space and %percent"
    uv_path = tmp_path / "bin" / "uv"

    definitions = render_systemd_definitions(root, tmp_path / "state", uv_path)

    resolved = str(root.resolve())
    for name in ("llm-wiki-nightly.service", "llm-wiki-weekly.service"):
        lines = definitions[name].decode().splitlines()
        working = [line for line in lines if line.startswith("WorkingDirectory=")]
        # systemd parses WorkingDirectory= verbatim: a quoted value is rejected
        # with "path is not absolute" and the unit never loads.
        assert working == [f"WorkingDirectory={resolved.replace('%', '%%')}"]


def _systemd_verify(unit_directory: Path) -> subprocess.CompletedProcess[str]:
    units = sorted(str(path) for path in unit_directory.iterdir())
    return subprocess.run(
        ["systemd-analyze", "--user", "verify", *units],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(
    sys.platform != "linux", reason="systemd unit verification is Linux-only"
)
def test_systemd_definitions_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze is unavailable")
    root = tmp_path / "vault with space"
    root.mkdir()
    uv_path = tmp_path / "uv"
    uv_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv_path.chmod(0o755)
    unit_directory = tmp_path / "units"
    unit_directory.mkdir()
    for name, data in render_systemd_definitions(root, root, uv_path).items():
        (unit_directory / name).write_bytes(data)

    completed = _systemd_verify(unit_directory)

    assert completed.returncode == 0, completed.stderr
    assert "path is not absolute" not in completed.stderr


def test_scheduler_definition_is_durable_before_external_file_mutation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    target = tmp_path / "external" / "llm-wiki-nightly.timer"
    definition = b"[Timer]\nPersistent=true\n"
    resource = file_resource(
        resource_id="systemd-nightly-timer",
        kind="systemd_unit",
        path=target,
        desired=definition,
        definition_path="scheduler/linux/llm-wiki-nightly.timer",
    )
    original_writer = resource.write_owned

    def write_owned(value: bytes | None) -> None:
        persisted = (
            state_root / "run" / "install" / "scheduler" / "linux" / "llm-wiki-nightly.timer"
        )
        assert persisted.read_bytes() == definition
        original_writer(value)

    resource = ManagedResource(
        resource_id=resource.resource_id,
        kind=resource.kind,
        locator=resource.locator,
        desired=resource.desired,
        read_owned=resource.read_owned,
        write_owned=write_owned,
        recognizes=resource.recognizes,
        metadata=resource.metadata,
        definitions=resource.definitions,
    )

    manifest = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="systemd_user",
        resources=[resource],
    )

    assert target.read_bytes() == definition
    assert manifest["resources"][0]["metadata"]["definition_path"] == (
        "scheduler/linux/llm-wiki-nightly.timer"
    )


def test_unknown_existing_scheduler_file_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "llm-wiki-nightly.timer"
    target.write_bytes(b"user-owned-definition")
    resource = file_resource(
        resource_id="systemd-nightly-timer",
        kind="systemd_unit",
        path=target,
        desired=b"llm-wiki-definition",
        definition_path="scheduler/linux/llm-wiki-nightly.timer",
    )

    with pytest.raises(Exception, match="ownership_ambiguous"):
        install_resources(
            state_root=tmp_path / "state",
            vault_root=tmp_path / "vault",
            release=_release(),
            scheduler_backend="systemd_user",
            resources=[resource],
        )

    assert target.read_bytes() == b"user-owned-definition"


def test_systemd_scheduler_resource_installs_verifies_and_uninstalls_as_one_unit(
    tmp_path: Path,
) -> None:
    runner = _FakeSystemd()

    state_root = tmp_path / "state"
    state_root.mkdir()
    unit_dir = tmp_path / "config" / "systemd" / "user"
    resource = systemd_scheduler_resource(
        root=tmp_path / "vault",
        state_root=state_root,
        uv_path=tmp_path / "bin" / "uv",
        unit_directory=unit_dir,
        runner=runner,
        systemctl="systemctl",
    )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="systemd_user",
        resources=[resource],
    )

    assert runner.enabled == {"llm-wiki-nightly.timer", "llm-wiki-weekly.timer"}
    assert {path.name for path in unit_dir.iterdir()} == {
        "llm-wiki-nightly.service",
        "llm-wiki-nightly.timer",
        "llm-wiki-weekly.service",
        "llm-wiki-weekly.timer",
    }
    internal = state_root / "run" / "install" / "scheduler" / "linux"
    assert {path.name for path in internal.iterdir()} == {
        "llm-wiki-nightly.service",
        "llm-wiki-nightly.timer",
        "llm-wiki-weekly.service",
        "llm-wiki-weekly.timer",
    }

    uninstall_resources(state_root=state_root, resources=[resource])

    assert runner.enabled == set()
    assert not list(unit_dir.glob("llm-wiki-*"))
    assert ("systemctl", "--user", "daemon-reload") in runner.calls


def test_launchd_scheduler_resource_installs_and_uninstalls_owned_agents(
    tmp_path: Path,
) -> None:
    runner = _FakeLaunchd()
    state_root = tmp_path / "state"
    state_root.mkdir()
    agents = tmp_path / "Library" / "LaunchAgents"
    resource = launchd_scheduler_resource(
        root=tmp_path / "vault",
        state_root=state_root,
        uv_path=tmp_path / "bin" / "uv",
        launch_agents_directory=agents,
        uid=501,
        runner=runner,
        launchctl="launchctl",
    )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="launchd",
        resources=[resource],
    )

    assert runner.active == {
        "io.github.ekgardt.llm-wiki.nightly",
        "io.github.ekgardt.llm-wiki.weekly",
    }
    assert {path.name for path in agents.iterdir()} == {
        "io.github.ekgardt.llm-wiki.nightly.plist",
        "io.github.ekgardt.llm-wiki.weekly.plist",
    }
    internal = state_root / "run" / "install" / "scheduler" / "macos"
    assert {path.name for path in internal.iterdir()} == {
        "io.github.ekgardt.llm-wiki.nightly.plist",
        "io.github.ekgardt.llm-wiki.weekly.plist",
    }

    uninstall_resources(state_root=state_root, resources=[resource])

    assert runner.active == set()
    assert not list(agents.glob("io.github.ekgardt.llm-wiki.*.plist"))


def test_explicit_cron_resource_preserves_unrelated_table_on_uninstall(
    tmp_path: Path,
) -> None:
    original = b"15 2 * * * /usr/local/bin/user-job\n"
    runner = _FakeCron(original)
    state_root = tmp_path / "state"
    state_root.mkdir()
    resource = cron_scheduler_resource(
        root=tmp_path / "vault",
        state_root=state_root,
        uv_path=tmp_path / "bin" / "uv",
        runner=runner,
        crontab="crontab",
    )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
    )

    assert runner.table is not None
    assert runner.table.startswith(original)
    assert runner.table.count(b"# LLM-Wiki-cron-start") == 1
    definition = state_root / "run" / "install" / "scheduler" / "cron" / "crontab.block"
    assert definition.read_bytes().startswith(b"# LLM-Wiki-cron-start")

    uninstall_resources(state_root=state_root, resources=[resource])

    assert runner.table == original


def test_windows_environment_resources_restore_absence_on_uninstall(
    tmp_path: Path,
) -> None:
    values: dict[str, str] = {}

    def read_value(name: str) -> str | None:
        return values.get(name)

    def write_value(name: str, value: str | None) -> None:
        if value is None:
            values.pop(name, None)
            return
        values[name] = value

    state_root = tmp_path / "state"
    state_root.mkdir()
    root = tmp_path / "vault"
    resources = windows_environment_resources(
        root,
        state_root,
        read_value=read_value,
        write_value=write_value,
    )

    install_resources(
        state_root=state_root,
        vault_root=root,
        release=_release(),
        scheduler_backend="task_scheduler",
        resources=resources,
    )

    assert values == {
        "LLM_WIKI_ROOT": str(root.resolve()),
        "LLM_WIKI_STATE_ROOT": str(state_root.resolve()),
    }

    uninstall_resources(state_root=state_root, resources=resources)

    assert values == {}


def test_windows_task_resource_uses_verified_script_state_for_rollback(
    tmp_path: Path,
) -> None:
    runner = _FakeWindowsTasks()
    state_root = tmp_path / "state"
    state_root.mkdir()
    resource = windows_task_scheduler_resource(
        root=tmp_path / "vault",
        state_root=state_root,
        uv_path=tmp_path / "bin" / "uv.exe",
        script_path=tmp_path / "install-scheduled-tasks.ps1",
        powershell="pwsh.exe",
        runner=runner,
    )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="task_scheduler",
        resources=[resource],
    )

    assert runner.state == "equivalent"
    definition = state_root / "run" / "install" / "scheduler" / "windows" / "tasks.json"
    parsed = json.loads(definition.read_text(encoding="utf-8"))
    assert parsed["tasks"][0]["name"] == "LLMWiki-Nightly"
    assert parsed["tasks"][1]["name"] == "LLMWiki-Weekly"

    uninstall_resources(state_root=state_root, resources=[resource])

    assert runner.state == "absent"


def test_windows_scheduler_script_exposes_fail_closed_machine_state() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "install-scheduled-tasks.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$StateJson" in script
    assert "function Get-LLMWikiScheduledTaskState" in script
    assert 'if ($currentState -eq "conflict")' in script
    assert 'if ($currentState -eq "equivalent")' in script
    assert "Register-ScheduledTask" in script
    registration = script.split("# --- Nightly task", 1)[1]
    assert "Unregister-ScheduledTask" not in registration.split("# --- Optional: run now", 1)[0]
    assert "-Force | Out-Null" not in registration


def test_status_cli_is_read_only_and_emits_bounded_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()

    exit_code = main(["status", "--state-root", str(state_root)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "manifest": "absent",
        "status": "absent",
        "transaction": "absent",
    }
    assert captured.err == ""
    assert not (state_root / "run" / "install").exists()


def test_native_installers_delegate_persistent_mutation_only_after_smoke() -> None:
    root = Path(__file__).parents[1]
    shell = (root / "install.sh").read_text(encoding="utf-8")
    powershell = (root / "install.ps1").read_text(encoding="utf-8")

    assert 'installer_config.py" profile' not in shell
    assert "crontab -l" not in shell
    assert 'install_control.py" install' in shell
    assert shell.index('ok "Production smoke passed"') < shell.index('install_control.py" install')
    assert "[Environment]::SetEnvironmentVariable" not in powershell
    assert '& ".\\scripts\\install-scheduled-tasks.ps1"' not in powershell
    assert '"scripts\\install_control.py"' in powershell
    smoke_index = powershell.index('Ok "Production smoke passed"')
    assert smoke_index < powershell.index('"scripts\\install_control.py"', smoke_index)


@pytest.mark.parametrize("command", ["rollback", "uninstall"])
def test_recovery_cli_commands_require_explicit_runtime_inputs(
    tmp_path: Path, command: str
) -> None:
    parsed = _parser().parse_args(
        [
            command,
            "--root",
            str(tmp_path / "vault"),
            "--state-root",
            str(tmp_path / "state"),
            "--uv-path",
            str(tmp_path / "uv"),
            "--profile",
            str(tmp_path / ".profile"),
            "--home",
            str(tmp_path / "home"),
        ]
    )

    assert parsed.command == command
    assert parsed.home == tmp_path / "home"


def test_install_cli_activates_v2_with_explicit_home(tmp_path: Path, monkeypatch) -> None:
    import install_control

    captured = {}
    resource = ManagedResource(
        resource_id="cli-resource",
        kind="test_value",
        locator="test://cli-resource",
        desired=b"desired",
        read_owned=lambda: None,
        write_owned=lambda _value: None,
        recognizes=lambda current: current == b"desired",
    )
    args = argparse.Namespace(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / "uv",
        scheduler="native",
        profile=tmp_path / ".profile",
        powershell_path=None,
        home=tmp_path / "home",
        cursor_hooks=True,
        antigravity_hooks=True,
    )
    monkeypatch.setattr(install_control, "_selected_backend", lambda _value: "cron")
    monkeypatch.setattr(
        install_control,
        "build_install_resources",
        lambda **kwargs: captured.setdefault("build", kwargs) and [resource],
    )
    monkeypatch.setattr(install_control, "build_release_identity", lambda _root: _release())

    def install(**kwargs):
        captured["install"] = kwargs
        return {"transaction_id": "a" * 32}

    monkeypatch.setattr(install_control, "install_resources", install)

    result = install_control._install_from_args(args)

    assert result["status"] == "committed"
    assert captured["build"]["home"] == (tmp_path / "home").resolve()
    assert captured["install"]["control_version"] == 2


def test_active_install_state_is_healthy_but_blocks_runtime_deletion(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def read_owned() -> bytes | None:
        return value

    def write_owned(updated: bytes | None) -> None:
        nonlocal value
        value = updated

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[
            ManagedResource(
                resource_id="health-value",
                kind="test_value",
                locator="test://health-value",
                desired=b"installed",
                read_owned=read_owned,
                write_owned=write_owned,
                recognizes=lambda current: current == b"installed",
            )
        ],
    )

    result = validate_install_state(state_root)

    assert result == {
        "codes": [],
        "deletion_codes": ["install_manifest_retained"],
        "health": "ok",
        "status": "active",
    }


def test_corrupt_install_transaction_fails_closed_without_path_disclosure(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "state" / "run" / "install"
    install_root.mkdir(parents=True)
    (install_root / "transaction.json").write_text("{not-json", encoding="utf-8")

    result = validate_install_state(tmp_path / "state")

    assert result == {
        "codes": ["install_state_corrupt"],
        "deletion_codes": ["install_state_corrupt"],
        "health": "error",
        "status": "corrupt",
    }
    assert str(tmp_path) not in json.dumps(result)


def test_doctor_runtime_check_reports_corrupt_install_state(tmp_path: Path) -> None:
    import doctor

    state_root = tmp_path / "state"
    for name in ("cache", "logs", "run"):
        (state_root / name).mkdir(parents=True, exist_ok=True)
    install_root = state_root / "run" / "install"
    install_root.mkdir()
    (install_root / "transaction.json").write_text("{not-json", encoding="utf-8")

    result = doctor._runtime_check(state_root)

    assert result["status"] == "error"
    assert result["details"]["codes"] == ["install_state_corrupt"]
    assert result["details"]["install"]["status"] == "corrupt"


def test_fresh_v2_install_persists_exact_owned_fragments_before_mutation(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None
    observed: list[dict[str, object]] = []

    def read_owned() -> bytes | None:
        return value

    def write_owned(updated: bytes | None) -> None:
        nonlocal value
        transaction = json.loads(
            (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
        )
        observed.append(transaction)
        value = updated

    resource = ManagedResource(
        resource_id="v2-value",
        kind="test_value",
        locator="test://v2-value",
        desired=b"v2-installed",
        read_owned=read_owned,
        write_owned=write_owned,
        recognizes=lambda current: current == b"v2-installed",
    )

    manifest = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
        control_version=2,
    )

    assert manifest["schema"] == "install-manifest/v2"
    assert manifest["generation"] == 1
    record = manifest["resources"][0]
    assert record["desired"]["preimage"].startswith("preimages/")
    assert record["origin"] == {"state": "absent"}
    assert record["rollback"] == {"state": "absent"}
    assert observed[0]["schema"] == "install-transaction/v2"
    assert observed[0]["operation"] == "install"
    assert observed[0]["resources"][0]["desired"]["preimage"]
    assert validate_install_state(state_root)["status"] == "active"


def test_v2_update_keeps_old_manifest_active_until_target_verifies(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def resource(desired: bytes, observed: list[bytes] | None = None) -> ManagedResource:
        def read_owned() -> bytes | None:
            return value

        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            if observed is not None:
                observed.append((state_root / "run" / "install" / "manifest.json").read_bytes())
            value = updated

        return ManagedResource(
            resource_id="update-value",
            kind="test_value",
            locator="test://update-value",
            desired=desired,
            read_owned=read_owned,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    first = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource(b"old")],
        control_version=2,
    )
    manifest_path = state_root / "run" / "install" / "manifest.json"
    old_manifest = manifest_path.read_bytes()
    observed: list[bytes] = []
    release = {**_release(), "project_version": "4.1.0"}

    updated = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=release,
        scheduler_backend="cron",
        resources=[resource(b"new", observed)],
        control_version=2,
    )

    assert observed == [old_manifest]
    assert first["generation"] == 1
    assert updated["generation"] == 2
    assert updated["resources"][0]["origin"] == {"state": "absent"}
    rollback = updated["resources"][0]["rollback"]
    assert rollback["preimage"].startswith("preimages/")
    assert (state_root / "run" / "install" / rollback["preimage"]).read_bytes() == b"old"
    assert updated["rollback_point"]["generation"] == 1
    assert value == b"new"


@pytest.mark.parametrize("crash_point", ["before_mutation", "after_mutation"])
def test_v2_update_resumes_interruption_around_external_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    store = _InterruptingValue(crash_point)

    def managed(desired: bytes) -> ManagedResource:
        return ManagedResource(
            resource_id="resumable-update",
            kind="test_value",
            locator="test://resumable-update",
            desired=desired,
            read_owned=store.read,
            write_owned=store.write,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    original_mutator = install_control._v2_mutate_resources
    if crash_point == "before_mutation":
        monkeypatch.setattr(
            install_control,
            "_v2_mutate_resources",
            lambda **_kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt("simulated process interruption")
            ),
        )
    store.enabled = True
    release = {**_release(), "project_version": "4.1.0"}

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release=release,
            scheduler_backend="cron",
            resources=[managed(b"new")],
            control_version=2,
        )

    transaction_path = state_root / "run" / "install" / "transaction.json"
    interrupted = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert interrupted["operation"] == "update"
    assert interrupted["state"] == "mutating"

    store.enabled = False
    monkeypatch.setattr(install_control, "_v2_mutate_resources", original_mutator)
    resumed = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=release,
        scheduler_backend="cron",
        resources=[managed(b"new")],
        control_version=2,
    )

    terminal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert terminal["id"] == interrupted["id"]
    assert terminal["state"] == "committed"
    assert resumed["generation"] == 2
    assert store.value == b"new"


def test_v2_update_resumes_after_manifest_publication(tmp_path: Path, monkeypatch) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def managed(desired: bytes) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated

        return ManagedResource(
            resource_id="published-update",
            kind="test_value",
            locator="test://published-update",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    release = {**_release(), "project_version": "4.1.0"}
    set_state = install_control._set_transaction_state

    def interrupt_commit(path, transaction, state):
        if state == "committed" and transaction["operation"] == "update":
            raise KeyboardInterrupt("crash after publication")
        set_state(path, transaction, state)

    monkeypatch.setattr(install_control, "_set_transaction_state", interrupt_commit)

    with pytest.raises(KeyboardInterrupt, match="after publication"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release=release,
            scheduler_backend="cron",
            resources=[managed(b"new")],
            control_version=2,
        )

    transaction_path = state_root / "run" / "install" / "transaction.json"
    interrupted = json.loads(transaction_path.read_text(encoding="utf-8"))
    published = json.loads(
        (state_root / "run" / "install" / "manifest.json").read_text(encoding="utf-8")
    )
    assert interrupted["state"] == "publishing"
    assert published["generation"] == 2

    monkeypatch.setattr(install_control, "_set_transaction_state", set_state)
    resumed = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=release,
        scheduler_backend="cron",
        resources=[managed(b"new")],
        control_version=2,
    )

    terminal = json.loads(transaction_path.read_text(encoding="utf-8"))
    assert terminal["id"] == interrupted["id"]
    assert terminal["state"] == "committed"
    assert resumed == published


def test_failed_v2_update_restores_resources_and_leaves_old_manifest_active(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def managed(desired: bytes, fail: bool = False) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated
            if fail and updated == b"new":
                raise RuntimeError("failed update")

        return ManagedResource(
            resource_id="failed-update",
            kind="test_value",
            locator="test://failed-update",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    old = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    manifest_path = state_root / "run" / "install" / "manifest.json"
    old_bytes = manifest_path.read_bytes()

    with pytest.raises(RuntimeError, match="failed update"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release={**_release(), "project_version": "4.1.0"},
            scheduler_backend="cron",
            resources=[managed(b"new", fail=True)],
            control_version=2,
        )

    transaction = json.loads(
        (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "reverted"
    assert manifest_path.read_bytes() == old_bytes
    assert json.loads(old_bytes) == old
    assert value == b"old"


def test_explicit_v2_rollback_restores_latest_committed_update(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def managed(desired: bytes) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated

        return ManagedResource(
            resource_id="committed-rollback",
            kind="test_value",
            locator="test://committed-rollback",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release={**_release(), "project_version": "4.1.0"},
        scheduler_backend="cron",
        resources=[managed(b"new")],
        control_version=2,
    )

    result = rollback_resources(
        state_root=state_root,
        resources=[managed(b"new")],
    )

    active = json.loads(
        (state_root / "run" / "install" / "manifest.json").read_text(encoding="utf-8")
    )
    assert result["operation"] == "rollback"
    assert result["state"] == "committed"
    assert active["generation"] == 3
    assert active["release"]["project_version"] == "4.0.0"
    assert active["rollback_point"] is None
    assert value == b"old"


def test_v2_uninstall_after_update_restores_pre_first_install_baseline(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def managed(desired: bytes) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated

        return ManagedResource(
            resource_id="uninstall-baseline",
            kind="test_value",
            locator="test://uninstall-baseline",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release={**_release(), "project_version": "4.1.0"},
        scheduler_backend="cron",
        resources=[managed(b"new")],
        control_version=2,
    )

    result = uninstall_resources(
        state_root=state_root,
        resources=[managed(b"new")],
    )

    assert result["schema"] == "install-transaction/v2"
    assert result["operation"] == "uninstall"
    assert result["state"] == "committed"
    assert value is None
    assert not (state_root / "run" / "install" / "manifest.json").exists()


def test_validated_v1_install_is_adopted_into_v2_without_rewriting_resource(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None
    writes = 0

    def write_owned(updated: bytes | None) -> None:
        nonlocal value, writes
        value = updated
        writes += 1

    resource = ManagedResource(
        resource_id="v1-adoption",
        kind="test_value",
        locator="test://v1-adoption",
        desired=b"installed",
        read_owned=lambda: value,
        write_owned=write_owned,
        recognizes=lambda current: current == b"installed",
    )
    v1 = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
    )
    writes_after_v1 = writes

    adopted = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource],
        control_version=2,
    )

    assert v1["schema"] == "install-manifest/v1"
    assert adopted["schema"] == "install-manifest/v2"
    assert adopted["generation"] == 2
    assert writes == writes_after_v1
    assert adopted["resources"][0]["origin"] == {"state": "absent"}
    rollback = adopted["resources"][0]["rollback"]
    assert (state_root / "run" / "install" / rollback["preimage"]).read_bytes() == b"installed"


def test_v2_update_drift_is_quarantined_without_overwriting_user_value(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None

    def managed(desired: bytes) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated

        return ManagedResource(
            resource_id="drift-value",
            kind="test_value",
            locator="test://drift-value",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new"},
        )

    old = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    value = b"user-drift"

    with pytest.raises(Exception, match="quarantined"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release={**_release(), "project_version": "4.1.0"},
            scheduler_backend="cron",
            resources=[managed(b"new")],
            control_version=2,
        )

    transaction = json.loads(
        (state_root / "run" / "install" / "transaction.json").read_text(encoding="utf-8")
    )
    active = json.loads(
        (state_root / "run" / "install" / "manifest.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "quarantined"
    assert active == old
    assert value == b"user-drift"
    assert validate_install_state(state_root)["status"] == "quarantined"


def test_v2_cursor_update_and_uninstall_never_store_unrelated_config_bytes(
    tmp_path: Path,
) -> None:
    from integration_hook_config import cursor_hooks_resource

    state_root = tmp_path / "state"
    state_root.mkdir()
    destination = tmp_path / ".cursor" / "hooks.json"
    destination.parent.mkdir()
    original = {
        "private": "DO-NOT-COPY-TO-RUNTIME",
        "version": 1,
        "hooks": {"afterFileEdit": [{"command": "user-format"}]},
    }
    destination.write_text(json.dumps(original), encoding="utf-8")
    old = cursor_hooks_resource(destination, {"sessionStart": [{"command": "llm-wiki old"}]})
    new = cursor_hooks_resource(destination, {"sessionStart": [{"command": "llm-wiki new"}]})

    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[old],
        control_version=2,
    )
    install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release={**_release(), "project_version": "4.1.0"},
        scheduler_backend="cron",
        resources=[new],
        control_version=2,
    )
    uninstall_resources(state_root=state_root, resources=[new])

    assert json.loads(destination.read_bytes()) == original
    preimages = (state_root / "run" / "install" / "preimages").glob("*.bin")
    assert all(b"DO-NOT-COPY-TO-RUNTIME" not in path.read_bytes() for path in preimages)


def test_interrupted_v2_update_rollback_uses_persisted_fragments_not_checkout_desired(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    value: bytes | None = None
    interrupt = False

    def managed(desired: bytes) -> ManagedResource:
        def write_owned(updated: bytes | None) -> None:
            nonlocal value
            value = updated
            if interrupt and updated == b"new":
                raise KeyboardInterrupt("interrupted update")

        return ManagedResource(
            resource_id="persisted-recovery",
            kind="test_value",
            locator="test://persisted-recovery",
            desired=desired,
            read_owned=lambda: value,
            write_owned=write_owned,
            recognizes=lambda current: current in {b"old", b"new", b"checkout"},
        )

    old = install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[managed(b"old")],
        control_version=2,
    )
    interrupt = True
    with pytest.raises(KeyboardInterrupt, match="interrupted update"):
        install_resources(
            state_root=state_root,
            vault_root=tmp_path / "vault",
            release={**_release(), "project_version": "4.1.0"},
            scheduler_backend="cron",
            resources=[managed(b"new")],
            control_version=2,
        )

    interrupt = False
    result = rollback_resources(
        state_root=state_root,
        resources=[managed(b"checkout")],
    )

    active = json.loads(
        (state_root / "run" / "install" / "manifest.json").read_text(encoding="utf-8")
    )
    assert result["state"] == "reverted"
    assert active == old
    assert value == b"old"


def test_v2_rejects_resource_adapter_without_persisted_recovery_support(
    tmp_path: Path,
) -> None:
    resource = ManagedResource(
        resource_id="unsafe-v2-adapter",
        kind="test_value",
        locator="test://unsafe-v2-adapter",
        desired=b"desired",
        read_owned=lambda: None,
        write_owned=lambda _value: None,
        recognizes=lambda _current: False,
        supports_v2_recovery=False,
    )

    with pytest.raises(Exception, match="recovery_unsupported"):
        install_resources(
            state_root=tmp_path / "state",
            vault_root=tmp_path / "vault",
            release=_release(),
            scheduler_backend="cron",
            resources=[resource],
            control_version=2,
        )

    assert not (tmp_path / "state" / "run" / "install").exists()


def _failing_v2_resource() -> ManagedResource:
    value: bytes | None = None

    def write_owned(updated: bytes | None) -> None:
        nonlocal value
        value = updated
        if updated is not None:
            raise RuntimeError("later resource failed")

    return ManagedResource(
        resource_id="later-failure",
        kind="test_value",
        locator="test://later-failure",
        desired=b"fail",
        read_owned=lambda: value,
        write_owned=write_owned,
        recognizes=lambda current: current == b"fail",
    )


@pytest.mark.parametrize(
    "scenario_builder",
    [_systemd_v2_scenario, _launchd_v2_scenario, _windows_tasks_v2_scenario],
    ids=["systemd", "launchd", "windows-tasks"],
)
def test_scheduler_v2_recovers_exact_legacy_definitions_across_lifecycle(
    tmp_path: Path, scenario_builder
) -> None:
    scenario = scenario_builder(tmp_path)
    state_root = scenario["state_root"]
    state_root.mkdir()
    current = scenario["current"]
    old = scenario["old"]
    new = scenario["new"]
    added_store = _InterruptingValue("never")
    added = ManagedResource(
        resource_id="added-with-v2",
        kind="test_value",
        locator="test://added-with-v2",
        desired=b"added",
        read_owned=added_store.read,
        write_owned=added_store.write,
        recognizes=lambda value: value == b"added",
    )
    arguments = {
        "state_root": state_root,
        "vault_root": scenario["vault"],
        "scheduler_backend": scenario["backend"],
    }

    install_resources(release=_release(), resources=[old], **arguments)
    assert current() == scenario["old_state"]

    with pytest.raises(RuntimeError, match="later resource failed"):
        install_resources(
            release={**_release(), "project_version": "4.1.0"},
            resources=[new, _failing_v2_resource()],
            control_version=2,
            **arguments,
        )

    assert current() == scenario["old_state"]

    install_resources(
        release={**_release(), "project_version": "4.1.0"},
        resources=[new, added],
        control_version=2,
        **arguments,
    )
    assert current() == scenario["new_state"]
    assert added_store.value == b"added"

    rollback_resources(state_root=state_root, resources=[new, added])
    assert current() == scenario["old_state"]
    assert added_store.value is None

    install_resources(
        release={**_release(), "project_version": "4.2.0"},
        resources=[new],
        control_version=2,
        **arguments,
    )
    assert current() == scenario["new_state"]

    uninstall_resources(state_root=state_root, resources=[new])
    assert current() in ({}, None)


@pytest.mark.parametrize(
    "scenario_builder",
    [_systemd_v2_scenario, _launchd_v2_scenario, _windows_tasks_v2_scenario],
    ids=["systemd", "launchd", "windows-tasks"],
)
def test_scheduler_v2_interrupted_update_rolls_back_persisted_legacy_definition(
    tmp_path: Path, scenario_builder
) -> None:
    scenario = scenario_builder(tmp_path)
    state_root = scenario["state_root"]
    state_root.mkdir()
    current = scenario["current"]
    store = _InterruptingValue("after_mutation")
    store.enabled = True
    interrupted = ManagedResource(
        resource_id="interrupt-after-scheduler",
        kind="test_value",
        locator="test://interrupt-after-scheduler",
        desired=b"interrupt",
        read_owned=store.read,
        write_owned=store.write,
        recognizes=lambda value: value == b"interrupt",
    )
    arguments = {
        "state_root": state_root,
        "vault_root": scenario["vault"],
        "scheduler_backend": scenario["backend"],
    }

    install_resources(release=_release(), resources=[scenario["old"]], **arguments)

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        install_resources(
            release={**_release(), "project_version": "4.1.0"},
            resources=[scenario["new"], interrupted],
            control_version=2,
            **arguments,
        )

    assert current() == scenario["new_state"]

    store.enabled = False
    rollback_resources(
        state_root=state_root,
        resources=[scenario["new"], interrupted],
    )
    assert current() == scenario["old_state"]


@pytest.mark.parametrize(
    "scenario_builder",
    [_systemd_v2_scenario, _launchd_v2_scenario],
    ids=["systemd", "launchd"],
)
def test_scheduler_v2_rejects_malformed_definition_bundle(tmp_path: Path, scenario_builder) -> None:
    resource = scenario_builder(tmp_path)["new"]
    malformed = install_control.canonical_json_bytes(
        {
            "definitions": {},
            "format": "scheduler-definitions/v1",
            "state": "enabled_active",
        }
    )

    with pytest.raises(Exception, match="definition_bundle_invalid"):
        resource.read_projections([malformed])


def test_windows_task_v2_rejects_untrusted_historical_spec_root(
    tmp_path: Path,
) -> None:
    scenario = _windows_tasks_v2_scenario(tmp_path)
    resource = scenario["new"]
    value = json.loads(resource.desired)
    value["root"] = str((tmp_path / "different-vault").resolve())
    untrusted = install_control.canonical_json_bytes(value)

    with pytest.raises(Exception, match="task_spec_invalid"):
        resource.read_projections([untrusted])
