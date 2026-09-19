"""An uninstall names the agent registrations it leaves, and Windows owns the plugin too.

Research: `docs/research/2026-09-17-an-uninstall-names-what-it-leaves.md`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import install_control  # noqa: E402

RELEASE = {
    "commit_oid": "a" * 40,
    "project_version": "0.0.0",
    "source_mode": "pinned_remote",
    "uv_lock_sha256": "b" * 64,
    "worktree_clean": True,
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    directory = tmp_path / "home"
    (directory / ".codex").mkdir(parents=True)
    (directory / ".config" / "opencode").mkdir(parents=True)
    return directory


def _register_everywhere(home: Path) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"llm-wiki": {}}}), encoding="utf-8")
    (home / ".codex" / "config.toml").write_text("[mcp_servers.llm-wiki]\n", encoding="utf-8")
    (home / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"mcp": {"llm-wiki": {}}}), encoding="utf-8"
    )


def test_every_agent_that_still_names_the_server_is_listed(home: Path) -> None:
    _register_everywhere(home)

    found = install_control.unowned_agent_registrations(home)

    assert [entry["agent"] for entry in found] == ["claude", "codex", "opencode"]


def test_an_agent_that_never_had_the_entry_is_not_listed(home: Path) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {}}}), encoding="utf-8")

    assert install_control.unowned_agent_registrations(home) == []


def test_the_uninstall_command_reports_them(home: Path, tmp_path: Path, monkeypatch) -> None:
    _register_everywhere(home)
    monkeypatch.setattr(install_control, "_selected_backend", lambda _requested: "cron")
    monkeypatch.setattr(install_control, "_posix_scheduler_resource", _file_scheduler(tmp_path))
    monkeypatch.setattr(install_control, "build_release_identity", lambda _root: RELEASE)
    args = argparse.Namespace(
        root=tmp_path / "vault", state_root=tmp_path / "state", uv_path=tmp_path / "uv", home=home,
        scheduler="cron", profile=home / ".profile", powershell_path=None,
        opencode_plugin=False, claude_settings=False, codex_hooks=False,
    )
    install_control._install_from_args(args)

    result = install_control._uninstall_from_args(args)

    assert [entry["path"] for entry in result["left_behind"]][0] == str(home / ".claude.json")


def _file_scheduler(tmp_path: Path):
    target = tmp_path / "cron.txt"

    def write(value: bytes | None) -> None:
        if value is None:
            target.unlink(missing_ok=True)
            return
        target.write_bytes(value)

    def build(**_arguments: object) -> install_control.ManagedResource:
        return install_control.ManagedResource(
            resource_id="cron-scheduler",
            kind="test_scheduler",
            locator=str(target),
            desired=b"cron",
            read_owned=lambda: target.read_bytes() if target.exists() else None,
            write_owned=write,
            recognizes=lambda current: current == b"cron",
        )

    return build


def test_the_windows_installer_lets_the_transaction_own_the_plugin() -> None:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert 'if ($openCodeDetected) { $installControlArgs += "--opencode-plugin" }' in text
