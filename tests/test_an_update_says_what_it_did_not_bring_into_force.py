"""An update brings the extras the operator chose and names the resources it did not re-render.

`uv sync --locked --inexact` keeps what is installed and names no extra, so a package added
to an extra never reached an installed vault; names were compared unnormalized and
self-references were dropped. Owned resources — units, task settings, hook blocks — are
rendered by the installer, never by a maintenance pass.

Research: `docs/research/2026-09-17-an-update-says-what-it-did-not-bring-into-force.md`,
`docs/research/2026-09-25-an-update-brings-the-extras-the-operator-chose.md`.
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
semantic = ["huggingface-hub>=0.34,<2", "numpy>=2.2.6,<3", "onnxruntime>=1.18,<2; python_version >= '3.11'"]
reranker = ["torch>=2.2,<3", "transformers>=4.44,<6"]
hybrid = ["numpy>=2.2.6,<3", "llm-wiki[semantic,reranker]"]
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
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root, _extras: True)
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


def test_an_extra_is_chosen_by_a_package_only_it_brings_under_its_canonical_name(checkout, monkeypatch) -> None:
    """`huggingface_hub` is how the environment spells `huggingface-hub`."""
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy", "huggingface-hub"})

    outcome = _advance(checkout, "scripts/search_memory.py")

    assert (outcome["status"], outcome["extras"], outcome["resources"]) == ("updated", ("semantic",), "current")


def test_an_aggregate_is_never_chosen_by_itself_but_its_parts_are(checkout, monkeypatch) -> None:
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy", "onnxruntime", "torch"})

    outcome = _advance(checkout, "uv.lock")

    assert outcome["extras"] == ("reranker", "semantic")


def test_a_shared_package_alone_chooses_nothing(checkout, monkeypatch) -> None:
    """`numpy` belongs to more than one extra, so it says nothing about the choice."""
    monkeypatch.setattr(self_update, "_installed_distributions", lambda: {"numpy"})

    outcome = _advance(checkout, "uv.lock")

    assert outcome["extras"] == ()


def test_the_sync_names_every_chosen_extra_in_one_inexact_call() -> None:
    command = self_update._sync_command(("reranker", "semantic"))

    assert command[: len(self_update.BASELINE_SYNC_COMMAND)] == self_update.BASELINE_SYNC_COMMAND
    assert command[len(self_update.BASELINE_SYNC_COMMAND) :] == ("--extra", "reranker", "--extra", "semantic")


def test_names_compare_as_the_packaging_specification_says() -> None:
    assert {self_update.canonical_name(name) for name in ("Huggingface_Hub", "huggingface.hub", "huggingface--hub")} == {
        "huggingface-hub"
    }


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
        "  update: dependencies synced with extras: hybrid",
        "  update: owned resources rerun_installer",
    ]


def test_a_refused_update_logs_nothing_extra() -> None:
    lines: list[str] = []

    scheduled_nightly._log_update_aftermath(lines.append, {"status": "skipped", "reason": "detached_head"})

    assert lines == []
