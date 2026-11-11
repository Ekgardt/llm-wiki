"""The vault advances its own checkout only when that cannot lose work."""
from __future__ import annotations

import sys
from pathlib import Path

from tests.self_update_helpers import (
    _commit,
    _repository,
    linked_clone,  # noqa: F401
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_an_unchanged_checkout_reports_itself_current(linked_clone) -> None:  # noqa: F811
    import self_update

    _upstream, clone = linked_clone

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "current"


def test_a_fast_forward_advances_the_checkout(linked_clone, monkeypatch) -> None:  # noqa: F811
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "feature.py", "value = 2\n")
    monkeypatch.setattr(self_update, "synced_dependencies", lambda _root, _extras: True)

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "updated"
    assert outcome["dependencies"] == "synced"
    assert (clone / "feature.py").is_file()


def test_a_file_the_owner_is_editing_stops_the_update(linked_clone) -> None:  # noqa: F811
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
    linked_clone, monkeypatch  # noqa: F811
) -> None:
    import self_update

    upstream, clone = linked_clone
    _commit(upstream, "feature.py", "value = 2\n")
    (clone / "product.py").write_text("value = 99\n", encoding="utf-8")
    monkeypatch.setattr(self_update, "synced_dependencies", lambda _root, _extras: True)

    outcome = self_update.update_checkout(clone)

    assert outcome["status"] == "updated"
    assert (clone / "product.py").read_text(encoding="utf-8") == "value = 99\n"


def test_a_diverged_branch_is_left_alone(linked_clone) -> None:  # noqa: F811
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
