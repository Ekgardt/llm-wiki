from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from install_state import InstallControlError, ManagedResource
from llm_wiki import install_resource_builder as builder

_Recorded = dict[str, dict[str, object]]


def _recording_builder(kind: str, recorded: _Recorded) -> Callable[..., ManagedResource]:
    def build(**options: object) -> ManagedResource:
        recorded[kind] = options
        return ManagedResource(
            resource_id=kind,
            kind=kind,
            locator=kind,
            desired=b"",
            read_owned=lambda: None,
            write_owned=lambda _value: None,
            recognizes=lambda _value: True,
        )

    return build


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Recorded:
    calls: _Recorded = {}
    for name in ("cron", "launchd", "systemd"):
        fake = _recording_builder(name, calls)
        monkeypatch.setattr(builder, f"{name}_scheduler_resource", fake)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(shutil, "which", lambda name: f"/managed/{name}")
    return calls


def _scheduler(backend: str, tmp_path: Path) -> ManagedResource:
    return builder.posix_scheduler_resource(
        backend=backend,
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / "uv",
        ownership_metadata={"cron-user-maintenance": {"cron_existed": False}},
    )


def test_an_unknown_scheduler_backend_is_refused(recorded: _Recorded, tmp_path: Path) -> None:
    with pytest.raises(InstallControlError, match="install_scheduler_backend_invalid"):
        _scheduler("at", tmp_path)
    assert recorded == {}


def test_cron_is_built_with_its_recorded_ownership(recorded: _Recorded, tmp_path: Path) -> None:
    assert _scheduler("cron", tmp_path).kind == "cron"
    assert recorded["cron"]["ownership_metadata"] == {"cron_existed": False}
    assert recorded["cron"]["uv_path"] == tmp_path / "uv"


def test_launchd_is_built_in_the_users_launch_agents(recorded: _Recorded, tmp_path: Path) -> None:
    assert _scheduler("launchd", tmp_path).kind == "launchd"
    launchd = recorded["launchd"]
    assert launchd["launch_agents_directory"] == tmp_path / "home" / "Library" / "LaunchAgents"
    assert launchd["launchctl"] == "/managed/launchctl"


def test_systemd_units_follow_an_absolute_xdg_config_home(
    recorded: _Recorded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _scheduler("systemd_user", tmp_path)
    assert recorded["systemd"]["unit_directory"] == tmp_path / "config" / "systemd" / "user"
    assert recorded["systemd"]["systemctl"] == "/managed/systemctl"


@pytest.mark.parametrize("configured", ["relative/config", ""])
def test_systemd_units_fall_back_to_home_without_an_absolute_xdg_config_home(
    recorded: _Recorded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", configured)
    _scheduler("systemd_user", tmp_path)
    expected = tmp_path / "home" / ".config" / "systemd" / "user"
    assert recorded["systemd"]["unit_directory"] == expected


def test_a_posix_install_without_a_profile_is_refused(recorded: _Recorded, tmp_path: Path) -> None:
    with pytest.raises(InstallControlError, match="install_profile_required"):
        builder.base_install_resources(
            backend="cron",
            root=tmp_path / "vault",
            state_root=tmp_path / "state",
            uv_path=tmp_path / "uv",
            profile=None,
            powershell_path=None,
            metadata={},
        )


def test_a_windows_install_without_powershell_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallControlError, match="install_powershell_path_required"):
        builder.base_install_resources(
            backend="task_scheduler",
            root=tmp_path / "vault",
            state_root=tmp_path / "state",
            uv_path=tmp_path / "uv",
            profile=None,
            powershell_path=None,
            metadata={},
        )


def test_a_windows_install_owns_its_environment_and_its_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    resources = builder.base_install_resources(
        backend="task_scheduler",
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / "uv",
        profile=None,
        powershell_path="pwsh",
        metadata={},
    )
    assert [(resource.resource_id, resource.kind) for resource in resources] == [
        ("llm_wiki_root", "user_environment"),
        ("llm_wiki_state_root", "user_environment"),
        ("windows-task-scheduler-maintenance", "windows_task_scheduler"),
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("platform", "expected"), [("win32", "windows"), ("darwin", "posix"), ("linux", "posix")]
)
def test_the_opencode_plugin_lands_where_its_platform_keeps_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, expected: str
) -> None:
    seen: list[str] = []

    def global_dir(home: Path, _xdg: str | None, *, platform: str) -> Path:
        seen.append(platform)
        return home / "opencode"

    monkeypatch.setattr("installer_config.opencode_global_dir", global_dir)
    monkeypatch.setattr(sys, "platform", platform)
    destination = builder.opencode_plugin_destination(tmp_path)
    assert destination == tmp_path / "opencode" / "plugins" / "llm-wiki-memory.js"
    assert seen == [expected]


def _record_codex_existed(monkeypatch: pytest.MonkeyPatch) -> list[bool | None]:
    seen: list[bool | None] = []

    def codex_resource(
        _destination: Path, _template: object, *, root: Path, config_existed: bool | None
    ) -> ManagedResource:
        seen.append(config_existed)
        return _recording_builder("codex", {})()

    monkeypatch.setattr("integration_hook_config.codex_hooks_resource", codex_resource)
    monkeypatch.setattr("integration_hook_config.codex_hooks_template", lambda _root: {})
    return seen


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, None),
        ({"codex-user-hooks": {"other": 1}}, None),
        ({"codex-user-hooks": {"config_existed": 1}}, True),
        ({"codex-user-hooks": {"config_existed": False}}, False),
    ],
    ids=["unrecorded", "recorded-without-flag", "recorded-true", "recorded-false"],
)
def test_codex_hooks_remember_whether_their_config_existed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata: dict[str, dict[str, object]],
    expected: bool | None,
) -> None:
    seen = _record_codex_existed(monkeypatch)
    resources = builder._selected_ide_hook_resources(
        tmp_path, tmp_path / "home", False, False, codex_hooks=True, metadata=metadata
    )
    assert [resource.kind for resource in resources] == ["codex"]
    assert seen == [expected]


_SHOW_ENVIRONMENT = ("/managed/systemctl", "--user", "show-environment")


@pytest.mark.parametrize(
    ("platform", "expected", "asked_manager"),
    [("linux", "systemd_user", [_SHOW_ENVIRONMENT]), ("darwin", "launchd", [])],
)
def test_the_native_backend_follows_the_platform_and_the_user_manager(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: str,
    asked_manager: list[tuple[str, ...]],
) -> None:
    asked: list[tuple[str, ...]] = []

    def answer(command: tuple[str, ...], _input: bytes | None) -> tuple[int, bytes]:
        asked.append(command)
        return 0, b""

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", lambda name: f"/managed/{name}")
    monkeypatch.setattr(builder, "default_command_runner", answer)
    assert builder.selected_backend("native") == expected
    assert asked == asked_manager


def test_a_linux_user_manager_that_does_not_answer_asks_for_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/managed/{name}")
    monkeypatch.setattr(builder, "default_command_runner", lambda _command, _input: (1, b""))
    with pytest.raises(ValueError, match="select cron explicitly"):
        builder.selected_backend("native")


def test_without_systemctl_linux_never_asks_the_user_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(builder, "default_command_runner", pytest.fail)
    with pytest.raises(ValueError, match="select cron explicitly"):
        builder.selected_backend("native")
    assert builder.selected_backend("cron") == "cron"
