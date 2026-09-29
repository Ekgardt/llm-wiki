"""A sync that names every extra and group the vault has is exact, so leftovers go.

The owner's vault kept 2.7 GB of CUDA wheels after torch moved to its CPU build: the
installer and the nightly update both synced with `--inexact`, which never removes
what the lock no longer names. Both now sync exactly and name the whole selection:
the default extras, the extras and the dependency groups the operator installed.
See docs/research/2026-09-29-a-sync-removes-what-the-lock-no-longer-names.md.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path

import self_update
from installer_config import DEFAULT_EXTRAS, uv_sync_arguments

from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[1]


def _environment_with(tmp_path: Path, *distributions: str) -> Path:
    """A real virtual environment whose site-packages holds these distributions' metadata."""
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, timeout=LONG_TIMEOUT)
    purelib = sysconfig.get_path("purelib", vars={"base": str(environment), "platbase": str(environment)})
    for name in distributions:
        info = Path(purelib) / f"{name}-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n", encoding="utf-8")
    return environment


def test_a_group_is_chosen_by_a_distribution_only_it_brings() -> None:
    assert (
        self_update.chosen_groups(ROOT, {"pytest": set()}),
        self_update.chosen_groups(ROOT, {"numpy": set()}),
    ) == (("dev",), ())


def test_the_update_syncs_exactly_and_names_the_extras_and_the_groups() -> None:
    command = self_update._sync_command(("full",), ("dev",))

    assert ("--inexact" in command, command[-4:]) == (False, ("--extra", "full", "--group", "dev"))


def test_the_installer_asks_the_environment_and_syncs_exactly(tmp_path: Path) -> None:
    environment = _environment_with(tmp_path, "pytest")

    _chosen, arguments = uv_sync_arguments(ROOT, str(environment))

    expected = [argument for extra in sorted(DEFAULT_EXTRAS) for argument in ("--extra", extra)]
    assert ("--inexact" in arguments, arguments[6:]) == (False, [*expected, "--group", "dev"])
