"""Building the resources one install asks for."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from install_state import (
    InstallControlError,
    ManagedResource,
)
from llm_wiki.install_cron import cron_scheduler_resource
from llm_wiki.install_launchd import launchd_scheduler_resource
from llm_wiki.install_profile import profile_resource
from llm_wiki.install_records import default_command_runner
from llm_wiki.install_scheduler_render import select_scheduler_backend
from llm_wiki.install_systemd import systemd_scheduler_resource
from llm_wiki.install_windows import windows_environment_resources, windows_task_scheduler_resource


def _systemd_user_available(systemctl: str | None) -> bool:
    if systemctl is None:
        return False
    exit_code, _output = default_command_runner((systemctl, "--user", "show-environment"), None)
    return exit_code == 0


def _xdg_systemd_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    if configured and Path(configured).is_absolute():
        return Path(configured) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _require_profile(profile: Path | None) -> Path:
    if profile is None:
        raise InstallControlError("install_profile_required")
    return profile


def posix_scheduler_resource(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    ownership_metadata: Mapping[str, Mapping[str, object]],
) -> ManagedResource:
    builders: dict[str, Callable[[], ManagedResource]] = {
        "cron": lambda: cron_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            ownership_metadata=ownership_metadata.get("cron-user-maintenance"),
        ),
        "launchd": lambda: launchd_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            launch_agents_directory=Path.home() / "Library" / "LaunchAgents",
            uid=os.getuid(),
            launchctl=shutil.which("launchctl") or "launchctl",
        ),
        "systemd_user": lambda: systemd_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            unit_directory=_xdg_systemd_directory(),
            systemctl=shutil.which("systemctl") or "systemctl",
        ),
    }
    try:
        return builders[backend]()
    except KeyError as exc:
        raise InstallControlError("install_scheduler_backend_invalid") from exc


def _config_platform() -> str:
    """`opencode_global_dir` speaks posix/windows, not `sys.platform` names."""
    if sys.platform.startswith("win"):
        return "windows"
    return "posix"


def opencode_plugin_destination(home: Path) -> Path:
    from installer_config import opencode_global_dir

    directory = opencode_global_dir(
        home, os.environ.get("XDG_CONFIG_HOME"), platform=_config_platform()
    )
    return directory / "plugins" / "llm-wiki-memory.js"


def _claude_settings_destination(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def codex_hooks_destination(home: Path) -> Path:
    return home / ".codex" / "hooks.json"


def _recorded_config_existed(
    metadata: Mapping[str, Mapping[str, object]], resource_id: str
) -> bool | None:
    recorded = metadata.get(resource_id)
    if not isinstance(recorded, Mapping) or "config_existed" not in recorded:
        return None
    return bool(recorded["config_existed"])


def _ide_hook_factories(
    root: Path,
    home: Path,
    state_root: Path | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> dict[str, Callable[[], ManagedResource]]:
    """One factory per owned host resource, built only when it is selected.

    The first two entries are removal-only. Cursor and Antigravity were retired on
    2026-08-26 and nothing selects them at install time any more, but a manifest
    written before that names them, and `_active_resource` fails closed on an id the
    code cannot supply — so uninstall and rollback still need to build them.
    """
    from integration_hook_config import (
        claude_settings_resource,
        claude_settings_template,
        codex_hooks_resource,
        codex_hooks_template,
        opencode_plugin_resource,
        retired_antigravity_hooks_resource,
        retired_cursor_hooks_resource,
    )

    return {
        "cursor-user-hooks": lambda: retired_cursor_hooks_resource(home),
        "antigravity-user-hooks": lambda: retired_antigravity_hooks_resource(home),
        "opencode-plugin": lambda: opencode_plugin_resource(
            root, opencode_plugin_destination(home)
        ),
        "claude-user-settings": lambda: claude_settings_resource(
            _claude_settings_destination(home),
            claude_settings_template(root),
            root,
            state_root or root,
            config_existed=_recorded_config_existed(metadata, "claude-user-settings"),
        ),
        "codex-user-hooks": lambda: codex_hooks_resource(
            codex_hooks_destination(home),
            codex_hooks_template(root),
            root=root,
            config_existed=_recorded_config_existed(metadata, "codex-user-hooks"),
        ),
    }


def _selected_ide_hook_resources(
    root: Path,
    home: Path,
    retired_cursor_hooks: bool,
    retired_antigravity_hooks: bool,
    opencode_plugin: bool = False,
    claude_settings: bool = False,
    codex_hooks: bool = False,
    state_root: Path | None = None,
    metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> list[ManagedResource]:
    wanted = {
        "cursor-user-hooks": retired_cursor_hooks,
        "antigravity-user-hooks": retired_antigravity_hooks,
        "opencode-plugin": opencode_plugin,
        "claude-user-settings": claude_settings,
        "codex-user-hooks": codex_hooks,
    }
    factories = _ide_hook_factories(root, home, state_root, metadata or {})
    return [factories[key]() for key, selected in wanted.items() if selected]


def _posix_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    profile: Path | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> list[ManagedResource]:
    return [
        profile_resource(
            _require_profile(profile),
            root,
            state_root,
            metadata.get("unix-profile"),
        ),
        posix_scheduler_resource(
            backend=backend,
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            ownership_metadata=metadata,
        ),
    ]


def _windows_install_resources(
    *,
    root: Path,
    state_root: Path,
    uv_path: Path,
    powershell_path: str | None,
) -> list[ManagedResource]:
    if powershell_path is None:
        raise InstallControlError("install_powershell_path_required")
    return [
        *windows_environment_resources(root, state_root),
        windows_task_scheduler_resource(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            script_path=root / "scripts" / "install-scheduled-tasks.ps1",
            powershell=powershell_path,
        ),
    ]


def base_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    profile: Path | None,
    powershell_path: str | None,
    metadata: Mapping[str, Mapping[str, object]],
) -> list[ManagedResource]:
    if backend == "task_scheduler":
        return _windows_install_resources(
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            powershell_path=powershell_path,
        )
    return _posix_install_resources(
        backend=backend,
        root=root,
        state_root=state_root,
        uv_path=uv_path,
        profile=profile,
        metadata=metadata,
    )


def build_install_resources(
    *,
    backend: str,
    root: Path,
    state_root: Path,
    uv_path: Path,
    home: Path,
    profile: Path | None,
    powershell_path: str | None,
    retired_cursor_hooks: bool = False,
    retired_antigravity_hooks: bool = False,
    opencode_plugin: bool = False,
    claude_settings: bool = False,
    codex_hooks: bool = False,
    ownership_metadata: Mapping[str, Mapping[str, object]] | None = None,
) -> list[ManagedResource]:
    metadata = ownership_metadata or {}
    return [
        *base_install_resources(
            backend=backend,
            root=root,
            state_root=state_root,
            uv_path=uv_path,
            profile=profile,
            powershell_path=powershell_path,
            metadata=metadata,
        ),
        *_selected_ide_hook_resources(
            root,
            home,
            retired_cursor_hooks,
            retired_antigravity_hooks,
            opencode_plugin,
            claude_settings,
            codex_hooks,
            state_root,
            metadata,
        ),
    ]


def selected_backend(requested: str) -> str:
    systemctl = shutil.which("systemctl")
    available = False
    if sys.platform == "linux":
        available = _systemd_user_available(systemctl)
    return select_scheduler_backend(sys.platform, requested, available)
