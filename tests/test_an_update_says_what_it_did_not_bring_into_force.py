"""An update names the extras it did not upgrade and the resources it did not re-render.

`uv sync --locked --inexact` keeps what is installed and names no extra, so a package that
reaches the vault through `hybrid`, `code-graph` or `reranker` stays at its old version when
the lock moves. Owned resources — units, task settings, hook blocks — are rendered by the
installer, never by a maintenance pass. Both were silent.

Research: `docs/research/2026-09-17-an-update-says-what-it-did-not-bring-into-force.md`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import scheduled_nightly
import self_update

PYPROJECT = """\
[project]
name = "llm-wiki"
version = "0.0.0"

[project.optional-dependencies]
hybrid = ["numpy>=2.2.6,<3", "sentence-transformers>=2.7,<6"]
reranker = ["onnxruntime>=1.18,<2; python_version >= '3.11'", "torch>=2.2,<3"]
full = ["llm-wiki[hybrid]"]
"""


def _git(root: Path, *arguments: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    done = subprocess.run(
        ["git", "-C", str(root), *identity, *arguments], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _write(root: Path, relative: str, body: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A checkout whose upstream is one commit ahead, ready to fast-forward."""
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root: True)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _write(upstream, "pyproject.toml", PYPROJECT.encode("utf-8"))
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    local = tmp_path / "vault"
    _git(tmp_path, "clone", "-q", str(upstream), str(local))
    return local


def _advance(checkout: Path, *changed: str) -> dict:
    upstream = checkout.parent / "upstream"
    for relative in changed:
        _write(upstream, relative, b"changed\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "two")
    return self_update.update_checkout(checkout)


def test_a_lock_that_moved_names_the_installed_extras(checkout, monkeypatch) -> None:
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy", "sentence-transformers"})

    outcome = _advance(checkout, "uv.lock")

    assert (outcome["status"], outcome["extras"], outcome["resources"]) == ("updated", ("hybrid",), "current")


def test_an_extra_whose_packages_are_not_all_installed_is_not_named(checkout, monkeypatch) -> None:
    """`numpy` alone must not make `hybrid` look installed."""
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy", "torch"})

    outcome = _advance(checkout, "uv.lock")

    assert outcome["extras"] == ()


def test_code_that_moved_without_the_lock_names_no_extra(checkout, monkeypatch) -> None:
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy", "sentence-transformers"})

    outcome = _advance(checkout, "scripts/search_memory.py")

    assert (outcome["extras"], outcome["resources"]) == ((), "current")


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("scripts/install_control.py", "rerun_installer"),
        ("scripts/install-scheduled-tasks.ps1", "rerun_installer"),
        ("integrations/claude-code/settings.json", "rerun_installer"),
        ("scripts/doctor.py", "current"),
    ],
)
def test_a_change_to_an_owned_resource_asks_for_the_installer(checkout, changed, expected) -> None:
    outcome = _advance(checkout, changed)

    assert outcome["resources"] == expected


def test_the_nightly_logs_what_was_not_brought_into_force() -> None:
    lines: list[str] = []
    outcome = {"status": "updated", "dependencies": "synced", "extras": ("hybrid",), "resources": "rerun_installer"}

    scheduled_nightly._log_update_aftermath(lines.append, outcome)

    assert lines == [
        "  update: dependencies synced; extras not upgraded: hybrid",
        "  update: owned resources rerun_installer",
    ]


def test_a_refused_update_logs_nothing_extra() -> None:
    lines: list[str] = []

    scheduled_nightly._log_update_aftermath(lines.append, {"status": "skipped", "reason": "detached_head"})

    assert lines == []
