"""Re-running the installer with a smaller or different set of owned things succeeds.

An update could only grow: a request that dropped a recorded resource, swapped the
scheduler, or moved the profile to another shell's file ended in
`install_resource_request_mismatch`. Research:
`docs/research/2026-09-17-a-rerun-may-ask-for-fewer-things.md`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import install_control  # noqa: E402
from install_control import ManagedResource  # noqa: E402

PLUGIN_SOURCE = "const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root\n"
BACKEND_OF = {"native": "systemd_user", "cron": "cron"}
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
def machine(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "vault" / "scripts").mkdir(parents=True)
    (tmp_path / "vault" / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        PLUGIN_SOURCE, encoding="utf-8"
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "sched").mkdir()
    monkeypatch.setattr(install_control, "_selected_backend", BACKEND_OF.__getitem__)
    monkeypatch.setattr(
        install_control,
        "_posix_scheduler_resource",
        lambda *, backend, **_rest: _file_scheduler(tmp_path / "sched", backend),
    )
    monkeypatch.setattr(install_control, "build_release_identity", lambda _root: RELEASE)
    return tmp_path


def _install(machine: Path, *, scheduler: str, profile: str, plugin: bool) -> dict[str, object]:
    args = argparse.Namespace(
        root=machine / "vault",
        state_root=machine / "state",
        uv_path=machine / "uv",
        home=machine / "home",
        scheduler=scheduler,
        profile=machine / "home" / profile,
        powershell_path=None,
        opencode_plugin=plugin,
        claude_settings=False,
        codex_hooks=False,
    )
    return install_control._install_from_args(args)


def _plugin(machine: Path) -> Path:
    return install_control._opencode_plugin_destination(machine / "home")


def test_a_rerun_without_an_agent_takes_its_plugin_back(machine: Path) -> None:
    _install(machine, scheduler="native", profile=".bashrc", plugin=True)
    installed = _plugin(machine).exists()

    result = _install(machine, scheduler="native", profile=".bashrc", plugin=False)

    assert (installed, _plugin(machine).exists(), result["replaced"]) == (True, False, True)


def test_a_rerun_with_the_other_scheduler_swaps_it(machine: Path) -> None:
    _install(machine, scheduler="native", profile=".bashrc", plugin=False)

    _install(machine, scheduler="cron", profile=".bashrc", plugin=False)

    left = sorted(path.name for path in (machine / "sched").iterdir())
    assert left == ["cron.txt"]


def test_a_changed_login_shell_moves_the_profile_block(machine: Path) -> None:
    _install(machine, scheduler="native", profile=".bashrc", plugin=False)

    _install(machine, scheduler="native", profile=".zshrc", plugin=False)

    home = machine / "home"
    old_has_block = (home / ".bashrc").exists() and b"LLM_WIKI_ROOT" in (home / ".bashrc").read_bytes()
    assert (old_has_block, b"LLM_WIKI_ROOT" in (home / ".zshrc").read_bytes()) == (False, True)


def test_the_same_request_is_not_a_replacement(machine: Path) -> None:
    _install(machine, scheduler="native", profile=".bashrc", plugin=True)

    result = _install(machine, scheduler="native", profile=".bashrc", plugin=True)

    assert result["replaced"] is False
