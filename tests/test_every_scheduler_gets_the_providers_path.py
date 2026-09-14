"""A scheduled run on macOS or under cron sees the provider CLIs beside uv, as systemd does.

The systemd unit got `PATH=<uv dir>:<default>` after the nightly compile found no
provider; the LaunchAgent and the cron line did not. Research:
`docs/research/2026-09-14-every-scheduler-gets-the-providers-path.md`.
"""
from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_control  # noqa: E402
import installer_config  # noqa: E402


def _cron_assignments(tmp_path: Path) -> dict[str, str]:
    command = installer_config.build_cron_command(
        root=tmp_path / "vault",
        state_root=tmp_path / "state",
        uv_path=tmp_path / "home/.local/bin/uv",
        kind="nightly",
        log_path=tmp_path / "state/logs/cron-nightly.log",
    )
    words = shlex.split(command)
    assignments = [word for word in words[1 : words.index("run") - 1] if "=" in word]
    return dict(word.split("=", 1) for word in assignments)


def test_the_cron_line_carries_the_path_and_the_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "claude")

    assignments = _cron_assignments(tmp_path)

    assert (assignments["PATH"], assignments["MEMORY_LLM_PROVIDER"]) == (
        f"{(tmp_path / 'home/.local/bin').resolve()}:/usr/bin:/bin",
        "claude",
    )


def test_the_launch_agent_carries_the_path(tmp_path):
    definitions = install_control.render_launchd_definitions(tmp_path / "vault", tmp_path / "state", tmp_path / "bin/uv")
    nightly = plistlib.loads(definitions["io.github.ekgardt.llm-wiki.nightly.plist"])

    assert nightly["EnvironmentVariables"]["PATH"] == f"{(tmp_path / 'bin').resolve()}:/usr/bin:/bin:/usr/sbin:/sbin"
