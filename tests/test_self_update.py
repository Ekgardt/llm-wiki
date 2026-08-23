"""The vault advances its own checkout only when that cannot lose work."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def _commit(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "--quiet", "-m", f"add {name}")


@pytest.fixture()
def linked_clone(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repository and a clone that tracks its main branch."""
    upstream = _repository(tmp_path / "upstream")
    _commit(upstream, "product.py", "value = 1\n")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(upstream), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return upstream, clone


def test_an_unchanged_checkout_reports_itself_current(linked_clone) -> None:
    import self_update

    _upstream, clone = linked_clone

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "current"


def test_a_fast_forward_advances_the_checkout(linked_clone, monkeypatch) -> None:
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "feature.py", "value = 2\n")
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root: True)

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "updated"
    assert outcome["dependencies"] == "synced"
    assert (clone / "feature.py").is_file()


def test_a_file_the_owner_is_editing_stops_the_update(linked_clone) -> None:
    """The rule is not "clean tree" — it is "not this file".

    This working tree is dirty almost always, because the runtime rewrites the
    tracked index and log on every compile. A clean-tree rule would be an off
    switch; the exact rule refuses only when the update and the owner reach for
    the same file.
    """
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "product.py", "value = 3\n")
    (clone / "product.py").write_text("value = 99\n", encoding="utf-8")

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "local_changes_conflict"
    assert outcome["paths"] == ["product.py"]
    assert (clone / "product.py").read_text(encoding="utf-8") == "value = 99\n"


def test_an_unrelated_local_change_does_not_stop_the_update(
    linked_clone, monkeypatch
) -> None:
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "feature.py", "value = 2\n")
    (clone / "product.py").write_text("value = 99\n", encoding="utf-8")
    monkeypatch.setattr(self_update, "_synced_dependencies", lambda _root: True)

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "updated"
    assert (clone / "product.py").read_text(encoding="utf-8") == "value = 99\n"


def test_a_diverged_branch_is_left_alone(linked_clone) -> None:
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "feature.py", "value = 2\n")
    _commit(clone, "local.py", "value = 4\n")

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "diverged_branch"
    assert not (clone / "feature.py").exists()


def test_a_checkout_without_a_remote_is_skipped(tmp_path: Path) -> None:
    import self_update

    root = _repository(tmp_path / "solo")
    _commit(root, "product.py", "value = 1\n")

    outcome = self_update.update_checkout(root)

    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "no_tracking_remote"
