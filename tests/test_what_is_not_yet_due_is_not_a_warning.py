"""A fresh install is graded by what is wrong, not by what has not happened yet.

Right after a healthy install, `doctor` called the missing first snapshot and the
optional, never-installed Pyright "degraded", so every install ended "with
warnings"; a nightly that never ran stayed "unknown" forever. Research:
`docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md`.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import doctor  # noqa: E402
import install_control  # noqa: E402
import pyright_profile  # noqa: E402
from install_control import ManagedResource  # noqa: E402

RELEASE = {
    "commit_oid": "a" * 40,
    "project_version": "0.0.0",
    "source_mode": "pinned_remote",
    "uv_lock_sha256": "b" * 64,
    "worktree_clean": True,
}


def _file_scheduler(directory: Path, backend: str) -> ManagedResource:
    """A scheduler that lives in a file, so no test touches crontab or systemd."""
    target = directory / f"{backend}.txt"

    def write(value: bytes | None) -> None:
        if value is None:
            target.unlink(missing_ok=True)
            return
        target.write_bytes(value)

    return ManagedResource(
        resource_id=f"{backend}-scheduler",
        kind="test_scheduler",
        locator=str(target),
        desired=backend.encode(),
        read_owned=lambda: target.read_bytes() if target.exists() else None,
        write_owned=write,
        recognizes=lambda current: current == backend.encode(),
    )


@pytest.fixture
def installed(tmp_path: Path, monkeypatch) -> Path:
    """A machine the real install transaction has just committed on."""
    (tmp_path / "vault" / "scripts").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "sched").mkdir()
    monkeypatch.setenv("LLM_WIKI_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    monkeypatch.setattr(install_control, "_selected_backend", lambda _mode: "systemd_user")
    monkeypatch.setattr(
        install_control,
        "_posix_scheduler_resource",
        lambda *, backend, **_rest: _file_scheduler(tmp_path / "sched", backend),
    )
    monkeypatch.setattr(install_control, "build_release_identity", lambda _root: RELEASE)
    install_control._install_from_args(
        argparse.Namespace(
            root=tmp_path / "vault",
            state_root=tmp_path / "state",
            uv_path=tmp_path / "uv",
            home=tmp_path / "home",
            scheduler="native",
            profile=tmp_path / "home" / ".bashrc",
            powershell_path=None,
            opencode_plugin=False,
            claude_settings=False,
            codex_hooks=False,
        )
    )
    return tmp_path


def _scheduler(machine: Path, now: datetime) -> dict:
    return doctor._scheduler_check(
        ROOT, machine / "state", now, time.monotonic() + 30, machine / "home"
    )


def _backup(machine: Path, now: datetime) -> dict:
    return doctor._backup_check(
        machine / "home", now, install_control.installed_at(machine / "state")
    )


def test_the_install_records_when_it_began(installed: Path) -> None:
    began = install_control.installed_at(installed / "state")
    assert began is not None
    assert abs((datetime.now(UTC) - began).total_seconds()) < 600


def test_a_fresh_install_has_nothing_due_yet(installed: Path) -> None:
    now = datetime.now(UTC)
    verdicts = (_scheduler(installed, now)["status"], _backup(installed, now)["status"])
    assert verdicts == ("ok", "ok")


def test_a_nightly_that_never_ran_is_named_once_it_was_due(installed: Path) -> None:
    later = datetime.now(UTC) + timedelta(seconds=doctor.FIRST_NIGHTLY_DUE_SECONDS + 60)
    check = _scheduler(installed, later)
    assert (check["status"], "never run" in check["message"]) == ("degraded", True)


def test_a_snapshot_that_never_came_is_named_once_it_was_due(installed: Path) -> None:
    later = datetime.now(UTC) + timedelta(seconds=doctor.BACKUP_FRESH_SECONDS + 60)
    assert _backup(installed, later)["status"] == "degraded"


def test_without_an_install_record_the_old_answers_stand(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_WIKI_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    now = datetime.now(UTC)
    verdicts = (_scheduler(tmp_path, now)["status"], _backup(tmp_path, now)["status"])
    assert verdicts == ("skipped", "degraded")


def _identity(status: str, codes: tuple[str, ...]) -> pyright_profile.PyrightIdentity:
    return pyright_profile.PyrightIdentity(
        status=status,
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
        degradation_codes=codes,
    )


def test_pyright_never_installed_is_an_optional_feature_not_taken() -> None:
    check = doctor._unqualified_pyright_result(
        _identity("missing", ("pyright_missing",)), {}, []
    )
    assert (check["status"], "install_pyright" in check["message"]) == ("skipped", True)


def test_pyright_missing_with_a_broken_setting_still_degrades() -> None:
    codes = ("pyright_missing", "pyright_repository_config_malformed")
    check = doctor._unqualified_pyright_result(_identity("missing", codes), {}, [])
    assert check["status"] == "degraded"
