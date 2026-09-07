"""Until 2026-09-02 the private memory existed in exactly one place.

Of 116 knowledge pages 83 are tracked in git; of 15 daily logs, 3. The rest is
private by design and therefore had no copy anywhere. The undo trail was not one
either — it held before-and-after images of individual writes, and a month of
them had reached 5.0 GB without ever being pruned, which is why it was cut to
307 MB the same day.

`knowledge/` is 20 MB and about 2.8 MB of it changes daily, so a second copy
with full history costs almost nothing. It lives outside the vault, so wiping
the vault does not take it along, and its repository has no remote — which is
the whole answer to whether it can leak.

See `knowledge/notes/memory-keeps-a-second-copy-decision.md`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import snapshot_knowledge  # noqa: E402


def _vault(tmp_path: Path, **pages: str) -> Path:
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for name, body in pages.items():
        (notes / f"{name}.md").write_text(body, encoding="utf-8")
    return vault


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout


def test_the_memory_is_copied_out_of_the_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="the user prefers tea")
    root = tmp_path / "snapshots"

    result = snapshot_knowledge.take_snapshot(vault, root)

    assert result["status"] == "ok"
    copied = root / "knowledge" / "notes" / "alpha.md"
    assert copied.read_text(encoding="utf-8") == "the user prefers tea"


def test_the_copy_has_no_remote_so_it_cannot_leak(tmp_path: Path) -> None:
    """The whole answer to 'will git send this somewhere': there is nowhere."""
    vault = _vault(tmp_path, alpha="private")
    root = tmp_path / "snapshots"

    snapshot_knowledge.take_snapshot(vault, root)

    assert _git(root, "remote").strip() == ""


def test_a_second_snapshot_of_unchanged_memory_adds_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="private")
    root = tmp_path / "snapshots"
    snapshot_knowledge.take_snapshot(vault, root)

    again = snapshot_knowledge.take_snapshot(vault, root)

    assert again["commit"] == "no change"
    assert len(_git(root, "log", "--oneline").splitlines()) == 1


def test_a_change_becomes_a_new_point_in_the_history(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="first")
    root = tmp_path / "snapshots"
    snapshot_knowledge.take_snapshot(vault, root)
    (vault / "knowledge" / "notes" / "alpha.md").write_text("second", encoding="utf-8")

    snapshot_knowledge.take_snapshot(vault, root)

    assert len(_git(root, "log", "--oneline").splitlines()) == 2


def test_an_earlier_version_can_be_read_back(tmp_path: Path) -> None:
    """A copy you cannot restore from is not a copy."""
    vault = _vault(tmp_path, alpha="first")
    root = tmp_path / "snapshots"
    snapshot_knowledge.take_snapshot(vault, root)
    (vault / "knowledge" / "notes" / "alpha.md").write_text("second", encoding="utf-8")
    snapshot_knowledge.take_snapshot(vault, root)

    earlier = _git(root, "show", "HEAD~1:knowledge/notes/alpha.md")

    assert earlier == "first"


def test_a_deleted_page_leaves_the_copy_but_stays_in_the_history(tmp_path: Path) -> None:
    """The copy mirrors today; the history is what holds yesterday."""
    vault = _vault(tmp_path, alpha="first", beta="also")
    root = tmp_path / "snapshots"
    snapshot_knowledge.take_snapshot(vault, root)
    (vault / "knowledge" / "notes" / "beta.md").unlink()

    snapshot_knowledge.take_snapshot(vault, root)

    assert not (root / "knowledge" / "notes" / "beta.md").exists()
    assert _git(root, "show", "HEAD~1:knowledge/notes/beta.md") == "also"


def test_a_vault_without_memory_is_not_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = snapshot_knowledge.take_snapshot(empty, tmp_path / "snapshots")

    assert result["status"] == "no memory to snapshot"


def test_the_copy_is_owner_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path, alpha="private")
    root = tmp_path / "snapshots"

    snapshot_knowledge.take_snapshot(vault, root)

    assert root.stat().st_mode & 0o077 == 0


def test_the_destination_is_outside_the_vault_by_default() -> None:
    """Wiping the vault directory must not take the second copy with it."""
    from memory_state import ROOT

    assert snapshot_knowledge.DEFAULT_SNAPSHOT_ROOT.resolve() != ROOT.resolve()
    assert ROOT.resolve() not in snapshot_knowledge.DEFAULT_SNAPSHOT_ROOT.resolve().parents
