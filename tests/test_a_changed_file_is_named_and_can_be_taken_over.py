"""A file changed outside the installer is named, and an edited schedule can be taken over.

The refusal used to be the bare code `install_resource_drift`: it named neither the
file nor the way on, and `--adopt` refused both schedulers, so a hand-edited unit (or
the doctor's own "rerun the installer" advice) led nowhere
(docs/research/2026-09-28-a-changed-file-is-named-and-can-be-taken-over.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import doctor
import install_control
import pytest
from install_control import (
    InstallControlError,
    install_resources,
    rollback_resources,
    systemd_scheduler_resource,
)

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_install_control import _FakeSystemd  # noqa: E402

_EDIT = "Environment=MEMORY_CLAUDE_MODEL=local-choice"
_SERVICE = "llm-wiki-nightly.service"


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
    return {path.name: path.read_bytes() for path in _units(tmp_path).iterdir()}


def _edited_install(tmp_path: Path) -> _FakeSystemd:
    """The schedule installed by 4.0.0, then one line added to a unit by hand."""
    (tmp_path / "state").mkdir()
    runner = _FakeSystemd()
    _install(tmp_path, "4.0.0", _schedule(tmp_path, runner, "old"))
    service = _units(tmp_path) / _SERVICE
    service.write_text(service.read_text(encoding="utf-8") + _EDIT + "\n", encoding="utf-8")
    return runner


def test_the_refusal_names_the_file_and_the_command_and_records_a_bare_code(
    tmp_path: Path,
) -> None:
    runner = _edited_install(tmp_path)

    with pytest.raises(InstallControlError) as refused:
        _install(tmp_path, "4.1.0", _schedule(tmp_path, runner, "new"))

    transaction = json.loads(
        (tmp_path / "state" / "run" / "install" / "transaction.json").read_bytes()
    )
    said = str(refused.value)
    assert (
        "systemd-user-maintenance" in said,
        str(_units(tmp_path)) in said,
        "--adopt systemd-user-maintenance" in said,
        transaction["error"] == {"code": refused.value.code},
    ) == (True, True, True, True)


def test_an_edited_schedule_is_taken_over_and_rollback_restores_the_edit(
    tmp_path: Path,
) -> None:
    runner = _edited_install(tmp_path)
    edited = _unit_files(tmp_path)
    [adopted] = install_control._marked_adopted(
        [_schedule(tmp_path, runner, "new")], ["systemd-user-maintenance"]
    )

    displaced = install_control._displaced_by_adoption([adopted])
    _install(tmp_path, "4.1.0", adopted)
    updated = _unit_files(tmp_path) == install_control.render_systemd_definitions(
        tmp_path / "vault", tmp_path / "state", tmp_path / "new" / "uv"
    )
    rollback_resources(state_root=tmp_path / "state", resources=[adopted])

    assert (updated, _EDIT in displaced["systemd-user-maintenance"], _unit_files(tmp_path)) == (
        True,
        True,
        edited,
    )


def test_the_doctor_advice_names_the_resource_the_installer_takes_over(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    directory = tmp_path / ".config" / "systemd" / "user"
    directory.mkdir(parents=True)
    (directory / _SERVICE).write_text("[Service]\nType=oneshot\n", encoding="utf-8")
    resource_id = _schedule(tmp_path, _FakeSystemd(), "uv").resource_id

    verdict = doctor._unit_limit_verdict(tmp_path)

    assert f"--adopt {resource_id}" in verdict[1]
