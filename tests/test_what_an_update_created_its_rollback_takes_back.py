"""What an update created, its rollback takes back; nothing else is touched.

After `test_an_update_replaces_what_it_owns.py` three gaps stayed: a rollback left the
drop-in its update wrote beside the restored unit (a moved `ExecStart` would run
twice), copies under `run/install/displaced/` were never retired, and a profile moved
to another file was still taken back before the new one was written
(docs/research/2026-09-28-what-an-update-created-its-rollback-takes-back.md).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import install_control
import install_takeover
import pytest
from install_control import ManagedResource

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_an_update_replaces_what_it_owns import (  # noqa: E402
    _ADDED,
    _add_line,
    _args,
    _drop_in,
    _machine,
    _manifest,
    _service_texts,
    _unit_files,
)

_OLD = 1_000_000.0


@pytest.fixture
def machine(tmp_path: Path, monkeypatch) -> dict[str, object]:
    return _machine(tmp_path, monkeypatch)


def _update_with_an_edited_unit(machine: dict[str, object]) -> dict[str, bytes]:
    """4.0.0 installed, a line added to the unit by hand, then 4.1.0 takes it over."""
    install_control._install_from_args(_args(machine, "old", plugin=False))
    _add_line(machine["path"])
    edited = _unit_files(machine["path"])
    machine["release"]["version"] = "4.1.0"
    install_control._install_from_args(_args(machine, "new", plugin=False))
    return edited


def _added_lines(machine: dict[str, object]) -> int:
    return "\n".join(_service_texts(machine["path"])).count(_ADDED)


def test_a_rollback_removes_the_drop_in_its_update_wrote(machine: dict[str, object]) -> None:
    edited = _update_with_an_edited_unit(machine)
    written = _drop_in(machine["path"]).exists()

    install_control._rollback_from_args(_args(machine, "new", plugin=False))

    assert (
        written,
        _unit_files(machine["path"]) == edited,
        _drop_in(machine["path"]).exists(),
        _added_lines(machine),
    ) == (True, True, False, 1)


def test_a_drop_in_that_was_there_before_stays_through_the_rollback(
    machine: dict[str, object],
) -> None:
    _drop_in(machine["path"]).parent.mkdir(parents=True)
    _drop_in(machine["path"]).write_text("[Service]\nNice=5\n", encoding="utf-8")
    _update_with_an_edited_unit(machine)

    install_control._rollback_from_args(_args(machine, "new", plugin=False))

    assert _drop_in(machine["path"]).read_text(encoding="utf-8") == "[Service]\nNice=5\n"


def test_a_later_update_and_its_rollback_leave_the_drop_in_to_the_operator(
    machine: dict[str, object],
) -> None:
    _update_with_an_edited_unit(machine)
    handed_over = _drop_in(machine["path"]).read_bytes()
    machine["release"]["version"] = "4.2.0"
    install_control._install_from_args(_args(machine, "newer", plugin=False))

    install_control._rollback_from_args(_args(machine, "newer", plugin=False))

    assert (_drop_in(machine["path"]).read_bytes(), _added_lines(machine)) == (handed_over, 1)


def _copy(root: Path, name: str, digest: str) -> Path:
    copy = root / name
    copy.mkdir(parents=True)
    (copy / "copy.json").write_text(json.dumps({"sha256": digest}), encoding="utf-8")
    os.utime(copy, (_OLD, _OLD))
    return copy


def test_old_copies_go_and_the_one_the_rollback_needs_stays(machine: dict[str, object]) -> None:
    _update_with_an_edited_unit(machine)
    displaced = machine["path"] / "state" / "run" / "install" / "displaced"
    [kept] = list(displaced.iterdir())
    os.utime(kept, (_OLD, _OLD))
    stale = _copy(displaced, "19700101T000000000000Z-systemd-user-maintenance", "0" * 64)

    retired = install_takeover.retire_displaced_copies(machine["path"] / "state")

    assert (retired, kept.exists(), stale.exists()) == ([stale], True, False)


def _profiles(machine: dict[str, object]) -> tuple[bool, bool]:
    home = machine["path"] / "home"
    return tuple(_holds_block(home / name) for name in (".bashrc", ".zshrc"))


def _holds_block(path: Path) -> bool:
    return path.exists() and b"LLM_WIKI_ROOT" in path.read_bytes()


def _profile_args(machine: dict[str, object], uv_dir: str, profile: str):
    args = _args(machine, uv_dir, plugin=False)
    args.profile = machine["path"] / "home" / profile
    return args


def test_a_moved_profile_is_written_before_the_old_block_goes(machine: dict[str, object]) -> None:
    install_control._install_from_args(_profile_args(machine, "old", ".bashrc"))
    machine["runner"].witness = lambda: _profiles(machine) == (True, True)
    machine["runner"].armed = True

    with pytest.raises(install_control.InstallControlError):
        install_control._install_from_args(_profile_args(machine, "new", ".zshrc"))
    after_failure = _profiles(machine)
    install_control._install_from_args(_profile_args(machine, "new", ".zshrc"))
    moved = _profiles(machine)
    install_control._rollback_from_args(_profile_args(machine, "new", ".zshrc"))

    assert (machine["runner"].witnessed, after_failure, moved, _profiles(machine)) == (
        [True],
        (True, False),
        (False, True),
        (True, False),
    )


def _file_cron(directory: Path) -> ManagedResource:
    """A cron entry kept in a file, so the test touches no crontab."""
    target = directory / "cron.txt"

    def write(value: bytes | None) -> None:
        if value is None:
            target.unlink(missing_ok=True)
            return
        target.write_bytes(value)

    return ManagedResource(
        resource_id="cron-user-maintenance",
        kind="cron_scheduler",
        locator=str(target),
        desired=b"cron",
        read_owned=lambda: target.read_bytes() if target.exists() else None,
        write_owned=write,
        recognizes=lambda current: current == b"cron",
    )


def test_a_change_to_cron_says_why_it_goes_first_and_names_the_command(
    machine: dict[str, object], monkeypatch, capsys
) -> None:
    install_control._install_from_args(_args(machine, "old", plugin=True))
    machine["plugin"].write_text("// changed by hand\n", encoding="utf-8")
    systemd = install_control._posix_scheduler_resource
    monkeypatch.setattr(install_control, "_selected_backend", lambda mode: {"cron": "cron"}.get(mode, "systemd_user"))
    monkeypatch.setattr(
        install_control,
        "_posix_scheduler_resource",
        lambda *, backend, **rest: _file_cron(machine["path"]) if backend == "cron" else systemd(backend=backend, **rest),
    )
    args = _args(machine, "old", plugin=True)
    args.scheduler = "cron"

    with pytest.raises(install_control.InstallControlError) as refused:
        install_control._install_from_args(args)

    said = str(refused.value)
    assert (
        "cannot be read back" in capsys.readouterr().err,
        str(machine["path"] / "cron.txt") in said,
        "install_control.py uninstall" in said,
        _manifest(machine)["scheduler_backend"],
    ) == (True, True, True, "systemd_user")
