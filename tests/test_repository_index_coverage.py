"""What a repository index covers, and what it says it covered.

The three gaps commit `27301e1` named and did not take: a tracked file at the
repository root was never indexed, this vault's prune vocabulary was applied to
every repository, and the walk under a root is a filesystem walk that the
receipt did not admit to. See
`docs/research/2026-08-29-what-an-index-looked-at.md`.

Every test builds a real Git repository in a temp directory and drives the real
code path. Nothing here touches the live vault, and nothing here writes into a
repository it did not create.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ALPHA = "def helper(value):\n    return value + 1\n"
README = "# Onboarding\n\nRun `bootstrap.sh` before the first deploy.\n"


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    return root


def _collect(repository: Path, roots: tuple[str, ...]):
    from corpus_snapshot import collect_corpus

    return collect_corpus(
        repository, code_roots=roots, approved_code_roots=roots, max_files=500
    )


# ------------------------------------------------------ gap 1: root files


def test_a_tracked_file_at_the_repository_root_is_a_code_root(tmp_path):
    """Seven names in the neighbouring repository were invisible; this is why."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {
            "src/alpha.py": ALPHA,
            "README.md": README,
            "pyproject.toml": "[project]\nname = 'x'\n",
        },
    )

    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("README.md", "pyproject.toml", "src")


def test_a_root_file_reaches_the_corpus_and_is_retrievable(tmp_path):
    """Selection is not the claim; the bytes arriving in the corpus are."""
    import repository_index

    repository = _repository(
        tmp_path / "repo", {"src/alpha.py": ALPHA, "README.md": README}
    )
    roots = repository_index.selected_code_roots(repository, None)

    snapshot = _collect(repository, roots.selected)

    paths = {source.record.relative_path for source in snapshot.sources}
    assert "README.md" in paths
    assert any(
        chunk.source_path == "README.md" and b"bootstrap.sh" in chunk.text.encode()
        for chunk in snapshot.chunks
    )


def test_a_hidden_root_file_is_excluded_by_name_not_dropped(tmp_path):
    """`.gitignore` is pruned by the same rule as `.github`, and is named for it."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, ".gitignore": "*.pyc\n", "README.md": README},
    )

    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("README.md", "src")
    assert roots.excluded == (".gitignore",)


def test_a_root_file_may_be_requested_explicitly(tmp_path):
    """A narrow index of one file is a legitimate request, not a missing root."""
    import repository_index

    repository = _repository(
        tmp_path / "repo", {"src/alpha.py": ALPHA, "README.md": README}
    )

    roots = repository_index.selected_code_roots(repository, ["README.md"])

    assert roots.selected == ("README.md",)


def test_a_tracked_root_symlink_is_excluded_rather_than_refusing_the_repository(
    tmp_path,
):
    """The collector refuses a symlinked path, so a root symlink would refuse all.

    Excluding it by name costs one entry; admitting it would cost the whole
    repository, and admitting it silently is the failure this module is about.
    """
    import repository_index

    repository = _repository(tmp_path / "repo", {"src/alpha.py": ALPHA})
    (repository / "link.md").symlink_to(repository / "src/alpha.py")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "link")

    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("src",)
    assert roots.excluded == ("link.md",)


def test_a_symlink_requested_explicitly_is_refused_by_name(tmp_path):
    import repository_index

    repository = _repository(tmp_path / "repo", {"src/alpha.py": ALPHA})
    (repository / "link.md").symlink_to(repository / "src/alpha.py")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "link")

    with pytest.raises(repository_index.RepositoryIndexRefused) as refusal:
        repository_index.selected_code_roots(repository, ["link.md"])

    assert refusal.value.reason == "repository_root_not_collectable"
    assert refusal.value.details["refused_roots"] == ["link.md"]


# ------------------------------------------- gap 2: whose vocabulary prunes


def test_a_foreign_directory_named_gaps_is_indexed_not_silently_lost(tmp_path):
    """`gaps` is an OKF page type in this vault and a directory name elsewhere."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {
            "gaps/finding.py": ALPHA,
            "src/raw-sources/beta.py": ALPHA,
            "src/_template/gamma.py": ALPHA,
        },
    )
    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("gaps", "src")
    assert roots.excluded == ()

    snapshot = _collect(repository, roots.selected)

    paths = {source.record.relative_path for source in snapshot.sources}
    assert paths == {
        "gaps/finding.py",
        "src/raw-sources/beta.py",
        "src/_template/gamma.py",
    }


def test_generated_and_hidden_directories_still_prune_everywhere(tmp_path):
    """The universal half of the old set does not move."""
    import repository_index

    repository = _repository(
        tmp_path / "repo",
        {"src/alpha.py": ALPHA, "src/__pycache__/alpha.pyc": "x", ".github/ci.yml": "on: push\n"},
    )
    roots = repository_index.selected_code_roots(repository, None)

    assert roots.selected == ("src",)
    assert roots.excluded == (".github",)

    snapshot = _collect(repository, roots.selected)

    paths = {source.record.relative_path for source in snapshot.sources}
    assert paths == {"src/alpha.py"}


# ------------------------------ gap 3: the receipt says which walk it was


def test_the_untracked_census_counts_what_git_does_not_track(tmp_path):
    """"Indexed" is not "tracked", and the difference is now a number."""
    import repository_index

    repository = _repository(tmp_path / "repo", {"src/alpha.py": ALPHA})
    (repository / "src/stray.py").write_text("z = 3\n", encoding="utf-8")
    roots = repository_index.selected_code_roots(repository, None)
    snapshot = _collect(repository, roots.selected)

    untracked = repository_index._untracked_sources(  # noqa: SLF001
        repository, snapshot
    )

    assert untracked == ("src/stray.py",)
    assert repository_index._untracked_source_count(  # noqa: SLF001
        repository, snapshot
    ) == 1


def test_the_census_is_zero_when_the_walk_and_git_agree(tmp_path):
    """Zero is an answer, and telling it apart from "not counted" is the point."""
    import repository_index

    repository = _repository(tmp_path / "repo", {"src/alpha.py": ALPHA})
    roots = repository_index.selected_code_roots(repository, None)
    snapshot = _collect(repository, roots.selected)

    assert repository_index._untracked_source_count(  # noqa: SLF001
        repository, snapshot
    ) == 0


def test_the_census_says_not_counted_rather_than_failing_the_build(tmp_path):
    """A build that already succeeded must not be failed by its own census."""
    import repository_index

    repository = _repository(tmp_path / "repo", {"src/alpha.py": ALPHA})
    roots = repository_index.selected_code_roots(repository, None)
    snapshot = _collect(repository, roots.selected)

    assert repository_index._untracked_source_count(  # noqa: SLF001
        tmp_path / "not-a-repository", snapshot
    ) is None
