"""What "old" means for a page, and what archiving remembers about it.

Three defects the MEM-12 forgetting stand measured on 2026-08-28
(`docs/research/2026-08-28-selective-forgetting-measured.md`):

* `NEW-127` — the archiver read `mtime` as the page's age, and this vault's own
  writers reset it. A checkout, an index rebuild or the nightly backlink pass
  rewrites bytes that are already identical, and every such touch restarts the
  forgetting clock. Measured on the live vault: 53 of 76 tracked notes carried
  an mtime more than a day newer than their last content change while
  byte-identical to HEAD, the largest gap 38 days.
* `NEW-126` — a page with no frontmatter whose body writes the literal
  `status:` was archived while declaring no retired status, and the legacy
  lexical index then answered it from inside the archive.
* `NEW-128` — restore deleted the `status:` line instead of restoring the word
  archiving found there, so a page archived as `status: preliminary` came back
  with no declared status at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DAY = 86400.0
DEFAULT_DAYS = 180


def _git(vault: Path, *arguments: str, when: float | None = None) -> None:
    stamp = {} if when is None else {
        "GIT_AUTHOR_DATE": f"@{int(when)} +0000",
        "GIT_COMMITTER_DATE": f"@{int(when)} +0000",
    }
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(vault),
        capture_output=True,
        text=True,
        env={**os.environ, **stamp},
        check=False,
    )
    assert completed.returncode == 0, f"git {arguments}: {completed.stderr}"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A throwaway vault wired into the archiver; never the live one."""
    import archive_stale

    root = tmp_path / "vault"
    notes = root / "knowledge" / "notes"
    notes.mkdir(parents=True)
    monkeypatch.setattr(archive_stale, "ROOT", root)
    monkeypatch.setattr(archive_stale, "KNOWLEDGE", notes)
    monkeypatch.setattr(archive_stale, "ARCHIVE_ROOT", notes / "archive")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    return root


def _repository(root: Path) -> None:
    if shutil.which("git") is None:  # pragma: no cover - git is a hard dependency here
        pytest.skip("git is not installed")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "stand@example.invalid")
    _git(root, "config", "user.name", "stand")
    _git(root, "config", "commit.gpgsign", "false")


def _page(root: Path, name: str, page_type: str = "debugging", body: str = "") -> Path:
    page = root / "knowledge" / "notes" / name
    page.write_text(
        f"---\ntype: {page_type}\n---\n\n# {name}\n\n{body or 'A lesson.'}\n",
        encoding="utf-8",
    )
    return page


def _commit(root: Path, page: Path, days_ago: float) -> None:
    _git(root, "add", "--", page.relative_to(root).as_posix())
    _git(root, "commit", "-q", "-m", page.name, when=time.time() - days_ago * DAY)


def _touch(page: Path, days_ago: float = 0.0) -> None:
    """What a checkout does: the bytes are untouched, the clock is not."""
    stamp = time.time() - days_ago * DAY
    os.utime(page, (stamp, stamp))


def _stale(root: Path) -> list[str]:
    import archive_stale

    cutoff = time.time() - DEFAULT_DAYS * DAY
    return sorted(page.name for page in archive_stale._stale_pages(cutoff, DEFAULT_DAYS))


# --------------------------------------------------------------------------
# NEW-127 — the recency signal
# --------------------------------------------------------------------------


def test_a_touched_page_keeps_the_age_of_its_last_content_change(vault):
    """The defect itself: a checkout-style touch used to reset the window."""
    _repository(vault)
    page = _page(vault, "old-lesson.md")
    _commit(vault, page, days_ago=100)
    _touch(page)

    assert _stale(vault) == ["old-lesson.md"]


def test_a_page_whose_bytes_differ_from_the_commit_is_dated_by_the_file(vault):
    """A page rewritten today is young, whatever its last commit says."""
    _repository(vault)
    page = _page(vault, "rewritten.md")
    _commit(vault, page, days_ago=100)
    page.write_text("---\ntype: debugging\n---\n\n# Rewritten today\n", encoding="utf-8")
    _touch(page)

    assert _stale(vault) == []


def test_an_untracked_page_still_ages_by_its_file_time(vault):
    """Private pages are not in git; mtime remains their only evidence."""
    _repository(vault)
    fresh = _page(vault, "private-fresh.md")
    old = _page(vault, "private-old.md")
    _touch(fresh)
    _touch(old, days_ago=100)

    assert _stale(vault) == ["private-old.md"]


def test_without_a_repository_the_file_time_decides(vault):
    """No git, no history: the archiver must still work, on mtime alone."""
    old = _page(vault, "no-repo-old.md")
    _touch(old, days_ago=100)

    assert _stale(vault) == ["no-repo-old.md"]


def test_a_recent_commit_never_makes_an_old_file_young_again(vault):
    """The oldest evidence wins, so the fix can only add archivable pages."""
    _repository(vault)
    page = _page(vault, "committed-today.md")
    _commit(vault, page, days_ago=0)
    _touch(page, days_ago=100)

    assert _stale(vault) == ["committed-today.md"]


def test_the_committed_content_map_only_vouches_for_unmodified_tracked_pages(vault):
    import archive_stale

    _repository(vault)
    clean = _page(vault, "clean.md")
    dirty = _page(vault, "dirty.md")
    _commit(vault, clean, days_ago=10)
    _commit(vault, dirty, days_ago=10)
    dirty.write_text("---\ntype: debugging\n---\n\n# Changed\n", encoding="utf-8")
    _page(vault, "untracked.md")

    known = archive_stale.committed_content_times(vault)

    assert set(known) == {"knowledge/notes/clean.md"}


def test_a_vault_nested_inside_a_repository_still_reads_its_history(tmp_path, monkeypatch):
    """git names paths from the repository root; the vault names them from its own."""
    import archive_stale

    repository = tmp_path / "repository"
    root = repository / "vault"
    (root / "knowledge" / "notes").mkdir(parents=True)
    monkeypatch.setattr(archive_stale, "ROOT", root)
    monkeypatch.setattr(archive_stale, "KNOWLEDGE", root / "knowledge" / "notes")
    monkeypatch.setattr(archive_stale, "ARCHIVE_ROOT", root / "knowledge/notes/archive")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path / "state"))
    _repository(repository)
    page = _page(root, "nested.md")
    _git(repository, "add", "--", "vault/knowledge/notes/nested.md")
    _git(repository, "commit", "-q", "-m", "nested", when=time.time() - 100 * DAY)
    _touch(page)

    known = archive_stale.committed_content_times(root)

    assert set(known) == {"knowledge/notes/nested.md"}
    assert known["knowledge/notes/nested.md"] == pytest.approx(
        time.time() - 100 * DAY, abs=120
    )
    assert _stale(root) == ["nested.md"]


def test_a_missing_repository_yields_no_committed_times(tmp_path):
    import archive_stale

    assert archive_stale.committed_content_times(tmp_path) == {}


def test_a_machine_without_git_still_archives_by_the_file_clock(vault, monkeypatch):
    """The signal fails open: no history is a fallback, never a refusal."""
    import archive_stale

    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    _repository(vault)
    old = _page(vault, "no-git-old.md")
    _commit(vault, old, days_ago=0)
    _touch(old, days_ago=100)
    monkeypatch.setattr(archive_stale.subprocess, "run", no_git)

    assert archive_stale.committed_content_times(vault) == {}
    assert _stale(vault) == ["no-git-old.md"]


# --------------------------------------------------------------------------
# NEW-126 — an archived page must declare that it is retired
# --------------------------------------------------------------------------


def _archived_copy(vault: Path, slug: str) -> Path:
    found = sorted((vault / "knowledge/notes/archive").rglob(f"{slug}.md"))
    assert found, f"{slug} was not archived"
    return found[0]


def test_a_page_whose_body_writes_status_is_archived_as_retired(vault):
    import archive_stale
    from page_status import is_retired

    page = vault / "knowledge/notes/body-mentions-status.md"
    page.write_text("A page with no frontmatter that writes status: here.\n", encoding="utf-8")

    assert archive_stale._archive_page(page, apply=True).startswith("ARCHIVED:")
    archived = _archived_copy(vault, "body-mentions-status").read_text(encoding="utf-8")
    assert archived.startswith("---\nstatus: archived\n---\n")
    assert is_retired("archived") and "status: archived" in archived


def test_such_an_archived_page_is_refused_by_the_legacy_index(vault):
    """The measured leak: `_collect_pages` does not skip the archive directory."""
    import archive_stale
    import search_memory

    page = vault / "knowledge/notes/body-mentions-status.md"
    page.write_text("A page with no frontmatter that writes status: here.\n", encoding="utf-8")
    archive_stale._archive_page(page, apply=True)

    collected = search_memory._collect_pages(
        knowledge_dir=vault / "knowledge/notes", root=vault
    )

    assert collected == []


def test_the_inserted_block_is_removed_again_on_restore(vault):
    import archive_stale

    page = vault / "knowledge/notes/body-mentions-status.md"
    original = "A page with no frontmatter that writes status: here.\n"
    page.write_text(original, encoding="utf-8")
    archive_stale._archive_page(page, apply=True)

    outcome = archive_stale.restore_page("body-mentions-status", apply=True)

    assert outcome.startswith("RESTORED:")
    assert page.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------
# NEW-128 — restore returns the status word archiving took
# --------------------------------------------------------------------------


def test_a_declared_status_survives_the_round_trip(vault):
    import archive_stale

    page = vault / "knowledge/notes/declared-status.md"
    original = "---\ntype: debugging\nstatus: preliminary\n---\n\nA page.\n"
    page.write_text(original, encoding="utf-8")

    archive_stale._archive_page(page, apply=True)
    archived = _archived_copy(vault, "declared-status").read_text(encoding="utf-8")
    archive_stale.restore_page("declared-status", apply=True)

    assert "status: archived" in archived
    assert "status_before_archive: preliminary" in archived
    assert page.read_text(encoding="utf-8") == original


def test_a_page_without_a_status_still_round_trips_byte_for_byte(vault):
    """The live cohort: 37 of 37 archivable pages declare no status at all."""
    import archive_stale

    page = vault / "knowledge/notes/no-status.md"
    original = "---\ntype: debugging\n---\n\nA page.\n"
    page.write_text(original, encoding="utf-8")

    archive_stale._archive_page(page, apply=True)
    archive_stale.restore_page("no-status", apply=True)

    assert page.read_text(encoding="utf-8") == original


def test_a_superseded_page_archived_by_hand_comes_back_superseded(vault):
    """`archived` is the only word not worth recording; history is not lost."""
    import archive_stale

    page = vault / "knowledge/notes/retired.md"
    original = "---\ntype: debugging\nstatus: superseded\n---\n\nA page.\n"
    page.write_text(original, encoding="utf-8")

    archive_stale._archive_page(page, apply=True)
    archive_stale.restore_page("retired", apply=True)

    assert page.read_text(encoding="utf-8") == original


def test_a_page_already_calling_itself_archived_records_no_previous_status(vault):
    """Otherwise restore would hand the page back still declaring `archived`."""
    import archive_stale

    page = vault / "knowledge/notes/already-archived.md"
    page.write_text(
        "---\ntype: debugging\nstatus: archived\n---\n\nA page.\n", encoding="utf-8"
    )

    archive_stale._archive_page(page, apply=True)
    archived = _archived_copy(vault, "already-archived").read_text(encoding="utf-8")
    archive_stale.restore_page("already-archived", apply=True)

    assert "status_before_archive" not in archived
    assert "status: archived" not in page.read_text(encoding="utf-8")
