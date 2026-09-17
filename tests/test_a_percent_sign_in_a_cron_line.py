"""A `%` in a path survives cron, which would otherwise cut the line there.

Research: `docs/research/2026-09-17-a-percent-sign-in-a-cron-line.md`.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from installer_config import build_cron_command  # noqa: E402

FAKE_UV = '#!/bin/sh\nprintf \'%s|%s\' "$LLM_WIKI_ROOT" "$5"\n'
# What cron hands to the shell after reading an escaped percent sign: implementations
# differ on whether the backslash is dropped.
CRON_READERS = {"drops the backslash": lambda line: line.replace("\\%", "%"), "keeps it": lambda line: line}


def _command(tmp_path: Path) -> tuple[str, Path, Path]:
    root = tmp_path / "vault 50% done"
    uv = tmp_path / "bin%dir" / "uv"
    for directory in (root, uv.parent):
        directory.mkdir()
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    log = root / "cron%.log"
    line = build_cron_command(root=root, state_root=root, uv_path=uv, kind="nightly", log_path=log)
    return line, root, log


def test_no_percent_sign_reaches_cron_unescaped(tmp_path: Path) -> None:
    line, _root, _log = _command(tmp_path)

    assert re.findall(r"(?<!\\)%", line) == []


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="needs a POSIX shell")
@pytest.mark.parametrize("reader", sorted(CRON_READERS))
def test_the_shell_receives_the_exact_paths(tmp_path: Path, reader: str) -> None:
    line, root, log = _command(tmp_path)

    subprocess.run(["sh", "-c", CRON_READERS[reader](line)], check=True, cwd=tmp_path)

    assert log.read_text(encoding="utf-8") == f"{root}|{root}"


def test_a_path_without_a_percent_sign_is_rendered_as_before(tmp_path: Path) -> None:
    import shlex

    line = build_cron_command(
        root=tmp_path, state_root=tmp_path, uv_path=tmp_path / "uv", kind="weekly", log_path=tmp_path / "w.log"
    )

    assert f"--directory {shlex.quote(str(tmp_path))} python scripts/scheduled_weekly.py" in line
