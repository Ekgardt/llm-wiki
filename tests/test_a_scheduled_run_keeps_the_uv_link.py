"""A scheduled run calls uv by the path the operator's PATH gives, links kept.

Resolving the link pinned Homebrew's versioned Cellar target, which `brew
upgrade` deletes. See docs/research/2026-09-25-a-scheduled-run-keeps-the-uv-link.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import install_control
import pytest
from installer_config import scheduled_path, stable_uv_path


@pytest.fixture
def linked_uv(tmp_path: Path) -> Path:
    if sys.platform == "win32":
        pytest.skip("symbolic links need privileges on Windows")
    target = tmp_path / "Cellar" / "uv" / "0.12.3" / "bin" / "uv"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "bin" / "uv"
    link.parent.mkdir()
    link.symlink_to(target)
    return link


def test_the_uv_path_keeps_its_link(linked_uv: Path) -> None:
    assert stable_uv_path(linked_uv) == linked_uv


def test_every_scheduler_calls_the_link_and_puts_its_directory_on_path(linked_uv: Path, tmp_path: Path) -> None:
    arguments = install_control._scheduled_arguments(tmp_path, linked_uv, "nightly")

    assert (arguments[0], scheduled_path(linked_uv, "/usr/bin").split(":")[0]) == (
        str(linked_uv),
        str(linked_uv.parent),
    )


def _installed_units(home: Path, uv: Path) -> None:
    import doctor

    for name, definition in install_control.render_systemd_definitions(home, home, uv).items():
        path = doctor._systemd_user_directory(home) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(definition)


def test_doctor_names_a_scheduled_uv_that_is_gone(linked_uv: Path, tmp_path: Path, monkeypatch) -> None:
    import doctor

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    home = tmp_path / "home"
    _installed_units(home, linked_uv)
    present = doctor._scheduled_program_verdict(home)
    linked_uv.unlink()

    verdict = doctor._scheduled_program_verdict(home)

    assert (present, verdict[0], str(linked_uv) in verdict[1]) == (None, "degraded", True)
