"""An update replaces the files the installer owns whole, and keeps what it displaces.

A unit edited by hand used to stop every update with `install_resource_drift` until the
operator chose `--adopt`; a set that dropped a resource was uninstalled before its
replacement was written. Now the edited unit is replaced, a readable copy of it is kept,
a line added by hand moves into a drop-in the installer never touches, a shared file
keeps its user's keys, and a smaller set is written before the old one is taken back
(docs/research/2026-09-28-an-update-replaces-what-it-owns.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import doctor
import install_control
import install_takeover
import pytest
from install_control import (
    install_resources,
    launchd_scheduler_resource,
    systemd_scheduler_resource,
)

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_install_control import _FakeLaunchd, _FakeSystemd  # noqa: E402

_SERVICE = "llm-wiki-nightly.service"
_ADDED = "Environment=MEMORY_CLAUDE_MODEL=local-choice"
_PLUGIN_SOURCE = "const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root\n"


class _Crash(BaseException):
    """The process dies mid-update: nothing reverts, the transaction stays open."""


class _FailingSystemd(_FakeSystemd):
    """systemd that refuses the next `enable` once armed, noting what else was there."""

    def __init__(self, witness: Path | None = None) -> None:
        super().__init__()
        self.armed = False
        self.crash = False
        self.witness = witness
        self.witnessed: list[bool] = []

    def _enable(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if self.crash:
            self.crash = False
            raise _Crash
        return self._refuse_once(command)

    def _refuse_once(self, command: tuple[str, ...]) -> tuple[int, bytes]:
        if not self.armed:
            return super()._enable(command)
        self.armed = False
        self.witnessed.append(self.witness is not None and self.witness.exists())
        return 1, b"refused\n"


def _release(version: str) -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": version,
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


def _units(tmp_path: Path) -> Path:
    return tmp_path / "systemd"


def _schedule(tmp_path: Path, runner: _FakeSystemd, uv_dir: str):
    return systemd_scheduler_resource(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / uv_dir / "uv",
        unit_directory=_units(tmp_path),
        runner=runner,
        systemctl="systemctl",
    )


def _install(tmp_path: Path, version: str, resource) -> dict:
    return install_resources(
        state_root=tmp_path / "state",
        vault_root=tmp_path / "vault",
        release=_release(version),
        scheduler_backend="systemd_user",
        resources=[resource],
        control_version=2,
    )


def _unit_files(tmp_path: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in _units(tmp_path).iterdir() if path.is_file()}


def _rendered(tmp_path: Path, uv_dir: str) -> dict[str, bytes]:
    return install_control.render_systemd_definitions(
        tmp_path / "vault", tmp_path / "state", tmp_path / uv_dir / "uv"
    )


def _edit_service(tmp_path: Path, replace_from: str, replace_to: str) -> None:
    service = _units(tmp_path) / _SERVICE
    text = service.read_text(encoding="utf-8")
    service.write_text(text.replace(replace_from, replace_to, 1), encoding="utf-8")


def _add_line(tmp_path: Path) -> None:
    """The hand edit: a model choice appended to the end of the [Service] section."""
    service = _units(tmp_path) / _SERVICE
    text = service.read_text(encoding="utf-8").rstrip("\n")
    service.write_text(f"{text}\n{_ADDED}\n", encoding="utf-8")


def _installed_and_edited(tmp_path: Path, runner: _FakeSystemd) -> None:
    (tmp_path / "state").mkdir(exist_ok=True)
    _install(tmp_path, "4.0.0", _schedule(tmp_path, runner, "old"))
    _add_line(tmp_path)


def _drop_in(tmp_path: Path) -> Path:
    return _units(tmp_path) / f"{_SERVICE}.d" / install_takeover.DROP_IN_NAME


def _service_texts(tmp_path: Path) -> list[str]:
    """The unit, then its drop-ins in name order: the order systemd parses them."""
    drop_ins = sorted((_units(tmp_path) / f"{_SERVICE}.d").glob("*.conf"))
    paths = [_units(tmp_path) / _SERVICE, *drop_ins]
    return [path.read_text(encoding="utf-8") for path in paths]


def _model_assignment(line: str) -> str | None:
    value = line.removeprefix("Environment=").strip('"')
    if not line.startswith("Environment=") or not value.startswith("MEMORY_CLAUDE_MODEL="):
        return None
    return value.split("=", 1)[1]


def _effective_model(tmp_path: Path) -> str | None:
    """The model the nightly service runs with: the last assignment systemd reads wins."""
    lines = "\n".join(_service_texts(tmp_path)).splitlines()
    assigned = [value for value in map(_model_assignment, lines) if value is not None]
    return assigned[-1] if assigned else None


def test_an_edited_unit_is_replaced_its_copy_kept_and_the_added_line_moves_to_a_drop_in(
    tmp_path: Path,
) -> None:
    runner = _FakeSystemd()
    _installed_and_edited(tmp_path, runner)
    edited, model_before = _unit_files(tmp_path), _effective_model(tmp_path)
    new = _schedule(tmp_path, runner, "new")

    [takeover] = install_takeover.keep_changed_whole_files(tmp_path / "state", [new])
    _install(tmp_path, "4.1.0", new)

    kept = Path(takeover.kept) / _SERVICE
    assert (
        _unit_files(tmp_path) == _rendered(tmp_path, "new"),
        kept.read_bytes() == edited[_SERVICE],
        "[Service]\n" + _ADDED in _drop_in(tmp_path).read_text(encoding="utf-8"),
        takeover.moved == [f"{_SERVICE}: {_ADDED}"],
        (model_before, _effective_model(tmp_path)),
    ) == (True, True, True, True, ("local-choice", "local-choice"))


def test_a_rerun_of_the_same_release_replaces_an_edited_unit(tmp_path: Path) -> None:
    runner = _FakeSystemd()
    _installed_and_edited(tmp_path, runner)
    again = _schedule(tmp_path, runner, "old")

    install_takeover.keep_changed_whole_files(tmp_path / "state", [again])
    manifest = _install(tmp_path, "4.0.0", again)

    assert (_unit_files(tmp_path) == _rendered(tmp_path, "old"), manifest["generation"]) == (
        True,
        2,
    )


def test_a_line_that_replaced_one_of_ours_is_reported_not_moved(tmp_path: Path) -> None:
    runner = _FakeSystemd()
    (tmp_path / "state").mkdir()
    _install(tmp_path, "4.0.0", _schedule(tmp_path, runner, "old"))
    _edit_service(tmp_path, "TimeoutStartSec=4h", "TimeoutStartSec=9h")
    new = _schedule(tmp_path, runner, "new")

    [takeover] = install_takeover.keep_changed_whole_files(tmp_path / "state", [new])

    assert (
        takeover.moved,
        takeover.unmoved,
        takeover.undone,
        _drop_in(tmp_path).exists(),
    ) == ([], [f"{_SERVICE}: TimeoutStartSec=9h"], [f"{_SERVICE}: TimeoutStartSec=4h"], False)


def test_a_drop_in_already_there_is_left_as_it_is(tmp_path: Path) -> None:
    runner = _FakeSystemd()
    _installed_and_edited(tmp_path, runner)
    _drop_in(tmp_path).parent.mkdir()
    _drop_in(tmp_path).write_text("[Service]\nNice=5\n", encoding="utf-8")

    [takeover] = install_takeover.keep_changed_whole_files(
        tmp_path / "state", [_schedule(tmp_path, runner, "new")]
    )

    assert (_drop_in(tmp_path).read_text(encoding="utf-8"), takeover.unmoved) == (
        "[Service]\nNice=5\n",
        [f"{_SERVICE}: {_ADDED}"],
    )


def _agents(tmp_path: Path, runner: _FakeLaunchd, uv_dir: str):
    return launchd_scheduler_resource(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / uv_dir / "uv",
        launch_agents_directory=tmp_path / "agents",
        uid=501,
        runner=runner,
        launchctl="launchctl",
    )


def test_an_edited_launch_agent_is_replaced_and_its_copy_is_what_keeps_the_edit(
    tmp_path: Path,
) -> None:
    """launchd has no drop-ins: the kept copy and the report are the whole answer."""
    runner = _FakeLaunchd()
    (tmp_path / "state").mkdir()
    install_resources(
        state_root=tmp_path / "state", vault_root=tmp_path / "vault", release=_release("4.0.0"),
        scheduler_backend="launchd", resources=[_agents(tmp_path, runner, "old")], control_version=2,
    )
    [plist] = sorted((tmp_path / "agents").glob("*nightly*.plist"))
    plist.write_text(plist.read_text(encoding="utf-8").replace(
        "<dict>", "<dict>\n<key>Nice</key><integer>5</integer>", 1), encoding="utf-8")
    edited = plist.read_bytes()
    new = _agents(tmp_path, runner, "new")

    [takeover] = install_takeover.keep_changed_whole_files(tmp_path / "state", [new])
    install_resources(
        state_root=tmp_path / "state", vault_root=tmp_path / "vault", release=_release("4.1.0"),
        scheduler_backend="launchd", resources=[new], control_version=2,
    )

    assert (
        (Path(takeover.kept) / plist.name).read_bytes() == edited,
        b"<key>Nice</key>" in plist.read_bytes(),
        takeover.unmoved == [f"{plist.name}: <key>Nice</key><integer>5</integer>"],
        takeover.drop_ins,
    ) == (True, False, True, [])


# --- The command the operator runs ----------------------------------------------------


@pytest.fixture
def machine(tmp_path: Path, monkeypatch) -> dict[str, object]:
    """A POSIX machine whose scheduler is systemd, faked at its command boundary."""
    (tmp_path / "vault" / "scripts").mkdir(parents=True)
    (tmp_path / "vault" / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        _PLUGIN_SOURCE, encoding="utf-8"
    )
    (tmp_path / "home").mkdir()
    plugin = install_control._opencode_plugin_destination(tmp_path / "home")
    runner = _FailingSystemd(witness=plugin)
    release = {"version": "4.0.0"}
    monkeypatch.setattr(install_control, "_selected_backend", lambda _mode: "systemd_user")
    monkeypatch.setattr(
        install_control,
        "_posix_scheduler_resource",
        lambda *, uv_path, **_rest: systemd_scheduler_resource(
            root=tmp_path / "vault",
            state_root=tmp_path / "state",
            uv_path=uv_path,
            unit_directory=_units(tmp_path),
            runner=runner,
            systemctl="systemctl",
        ),
    )
    monkeypatch.setattr(
        install_control, "build_release_identity", lambda _root: _release(release["version"])
    )
    return {"path": tmp_path, "runner": runner, "release": release, "plugin": plugin}


def _args(machine: dict[str, object], uv_dir: str, plugin: bool) -> argparse.Namespace:
    path = machine["path"]
    return argparse.Namespace(
        root=path / "vault",
        state_root=path / "state",
        uv_path=path / uv_dir / "uv",
        home=path / "home",
        scheduler="native",
        profile=path / "home" / ".bashrc",
        powershell_path=None,
        opencode_plugin=plugin,
        claude_settings=False,
        codex_hooks=False,
        adopt=[],
    )


def _manifest(machine: dict[str, object]) -> dict:
    path = machine["path"] / "state" / "run" / "install" / "manifest.json"
    return json.loads(path.read_bytes())


def test_a_failed_update_puts_the_edited_unit_back_and_withdraws_the_drop_in(
    machine: dict[str, object],
) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=False))
    _add_line(machine["path"])
    edited = _unit_files(machine["path"])
    machine["runner"].armed = True
    machine["release"]["version"] = "4.1.0"

    with pytest.raises(install_control.InstallControlError):
        install_control._install_from_args(_args(machine, "new", plugin=False))

    transaction = json.loads(
        (machine["path"] / "state" / "run" / "install" / "transaction.json").read_bytes()
    )
    assert (
        (transaction["state"], transaction["error"]["code"]),
        _unit_files(machine["path"]) == edited,
        _drop_in(machine["path"]).exists(),
        _manifest(machine)["generation"],
    ) == (("reverted", "install_scheduler_command_failed"), True, False, 1)


def test_a_smaller_set_is_written_before_the_old_one_is_taken_back(
    machine: dict[str, object],
) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=True))
    machine["runner"].armed = True

    with pytest.raises(install_control.InstallControlError):
        install_control._install_from_args(_args(machine, "new", plugin=False))
    dropped = install_control._install_from_args(_args(machine, "new", plugin=False))

    ids = [record["id"] for record in _manifest(machine)["resources"]]
    assert (
        machine["runner"].witnessed,
        dropped["replaced"],
        machine["plugin"].exists(),
        "opencode-plugin" in ids,
    ) == ([True], True, False, False)


def test_a_rerun_finishes_a_smaller_set_the_process_died_in(machine: dict[str, object]) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=True))
    machine["runner"].crash = True

    with pytest.raises(_Crash):
        install_control._install_from_args(_args(machine, "new", plugin=False))
    install_control._install_from_args(_args(machine, "new", plugin=False))

    ids = [record["id"] for record in _manifest(machine)["resources"]]
    assert (machine["plugin"].exists(), "opencode-plugin" in ids) == (False, False)


def test_a_rollback_after_a_smaller_set_puts_the_dropped_resource_back(
    machine: dict[str, object],
) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=True))
    installed = machine["plugin"].read_bytes()
    install_control._install_from_args(_args(machine, "new", plugin=False))

    install_control._rollback_from_args(_args(machine, "old", plugin=False))

    ids = [record["id"] for record in _manifest(machine)["resources"]]
    assert (machine["plugin"].read_bytes() == installed, "opencode-plugin" in ids) == (True, True)


def test_adopting_a_wholly_owned_file_still_works_and_says_it_is_not_needed(
    machine: dict[str, object], capsys
) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=False))
    _add_line(machine["path"])
    args = _args(machine, "new", plugin=False)
    args.adopt = ["systemd-user-maintenance"]

    install_control._install_from_args(args)

    assert (
        _unit_files(machine["path"]) == _rendered(machine["path"], "new"),
        "--adopt systemd-user-maintenance is no longer needed" in capsys.readouterr().err,
    ) == (True, True)


# --- A shared file, and the advice ----------------------------------------------------


def test_a_shared_settings_file_keeps_the_users_keys_through_an_update(
    tmp_path: Path, monkeypatch
) -> None:
    from integration_hook_config import claude_settings_resource, claude_settings_template

    settings = tmp_path / "home" / ".claude" / "settings.json"
    template = claude_settings_template(Path(__file__).resolve().parents[1])
    (tmp_path / "state").mkdir()

    def resource():
        return claude_settings_resource(settings, template, tmp_path / "vault", tmp_path / "state")

    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")
    monkeypatch.setenv("MEMORY_CLAUDE_MODEL", "first-choice")
    install_resources(
        state_root=tmp_path / "state", vault_root=tmp_path / "vault", release=_release("4.0.0"),
        scheduler_backend="systemd_user", resources=[resource()], control_version=2,
    )
    config = json.loads(settings.read_bytes())
    config["theme"] = "dark"
    config["env"]["USER_OWN"] = "kept"
    settings.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("MEMORY_CLAUDE_MODEL", "second-choice")

    install_takeover.keep_changed_whole_files(tmp_path / "state", [resource()])
    install_resources(
        state_root=tmp_path / "state", vault_root=tmp_path / "vault", release=_release("4.1.0"),
        scheduler_backend="systemd_user", resources=[resource()], control_version=2,
    )

    updated = json.loads(settings.read_bytes())
    assert (updated["theme"], updated["env"]["USER_OWN"], updated["env"]["MEMORY_CLAUDE_MODEL"]) == (
        "dark",
        "kept",
        "second-choice",
    )


def test_the_doctor_says_to_rerun_the_installer_and_where_an_edit_goes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    directory = tmp_path / ".config" / "systemd" / "user"
    directory.mkdir(parents=True)
    (directory / _SERVICE).write_text("[Service]\nType=oneshot\n", encoding="utf-8")

    message = doctor._unit_limit_verdict(tmp_path)[1]

    assert ("rerun the installer" in message, "--adopt" in message, "50-local.conf" in message) == (
        True,
        False,
        True,
    )
