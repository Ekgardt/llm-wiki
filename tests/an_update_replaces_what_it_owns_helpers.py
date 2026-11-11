"""Helpers shared by test_an_update_replaces_what_it_owns.py and the test files importing them."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import install_takeover
import pytest
from llm_wiki import install_release, install_resource_builder
from llm_wiki.install_systemd import systemd_scheduler_resource

from tests.install_control_helpers import _FakeSystemd

_SERVICE = "llm-wiki-nightly.service"


_ADDED = "Environment=MEMORY_CLAUDE_MODEL=local-choice"


_PLUGIN_SOURCE = "const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root\n"


class _Crash(BaseException):
    """The process dies mid-update: nothing reverts, the transaction stays open."""


class _FailingSystemd(_FakeSystemd):
    """systemd that refuses the next `enable` once armed, noting what else was there."""

    def __init__(self, witness: Callable[[], bool] | None = None) -> None:
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
        self.witnessed.append(self.witness is not None and self.witness())
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


def _unit_files(tmp_path: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in _units(tmp_path).iterdir() if path.is_file()}


def _add_line(tmp_path: Path) -> None:
    """The hand edit: a model choice appended to the end of the [Service] section."""
    service = _units(tmp_path) / _SERVICE
    text = service.read_text(encoding="utf-8").rstrip("\n")
    service.write_text(f"{text}\n{_ADDED}\n", encoding="utf-8")


def _drop_in(tmp_path: Path) -> Path:
    return _units(tmp_path) / f"{_SERVICE}.d" / install_takeover.DROP_IN_NAME


def _service_texts(tmp_path: Path) -> list[str]:
    """The unit, then its drop-ins in name order: the order systemd parses them."""
    drop_ins = sorted((_units(tmp_path) / f"{_SERVICE}.d").glob("*.conf"))
    paths = [_units(tmp_path) / _SERVICE, *drop_ins]
    return [path.read_text(encoding="utf-8") for path in paths]


@pytest.fixture
def machine(tmp_path: Path, monkeypatch) -> dict[str, object]:
    return _machine(tmp_path, monkeypatch)


def _machine(tmp_path: Path, monkeypatch) -> dict[str, object]:
    """A POSIX machine whose scheduler is systemd, faked at its command boundary."""
    (tmp_path / "vault" / "scripts").mkdir(parents=True)
    (tmp_path / "vault" / "scripts" / "llm-wiki-memory-opencode.js").write_text(
        _PLUGIN_SOURCE, encoding="utf-8"
    )
    (tmp_path / "home").mkdir()
    plugin = install_resource_builder.opencode_plugin_destination(tmp_path / "home")
    runner = _FailingSystemd(witness=plugin.exists)
    release = {"version": "4.0.0"}
    monkeypatch.setattr(install_resource_builder, "selected_backend", lambda _mode: "systemd_user")
    monkeypatch.setattr(
        install_resource_builder,
        "posix_scheduler_resource",
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
        install_release, "build_release_identity", lambda _root: _release(release["version"])
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
