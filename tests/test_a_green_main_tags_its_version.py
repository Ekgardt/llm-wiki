"""A green main commit tags the version it declares, once.

The tag was a manual step after the merge, and a month of changes went untagged
while the READMEs already named the next tag. See
docs/research/2026-09-24-a-green-main-tags-its-version.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import release_manifest
import release_tag

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [2.0.0] — 2026-09-24\n\n### Fixed\n\n- A thing.\n\n## [1.0.0] — 2026-01-01\n\n- Old.\n"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, version: str, changelog: str = CHANGELOG) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    for path in release_manifest.PINNED_FILES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"{path}\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release")
    return root, _git(root, "rev-parse", "HEAD")


def test_the_project_version_is_read_from_the_project_table() -> None:
    pyproject = '[tool.x]\nversion = "9"\n\n[project]\nname = "x"\nversion = "5.0.0"\n'

    assert release_tag.project_version(pyproject) == "5.0.0"
    assert release_tag.project_version('[tool.x]\nversion = "9"\n') is None


def test_a_section_ends_at_the_next_version() -> None:
    assert release_tag.changelog_section(CHANGELOG, "2.0.0") == "### Fixed\n\n- A thing."
    assert release_tag.changelog_section(CHANGELOG, "3.0.0") is None


def test_a_declared_version_with_a_section_is_tagged_once(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path, "2.0.0")

    planned = release_tag.planned_release(root, commit)
    release_tag.publish(root, planned, push=False)
    again = release_tag.planned_release(root, commit)

    tagged = (_git(root, "rev-parse", "v2.0.0^{commit}"), _git(root, "cat-file", "-t", "v2.0.0"))
    carried = [text in planned.message for text in ("- A thing.", commit)]

    assert (planned.tag, tagged, carried, again) == (
        "v2.0.0",
        (commit, "tag"),
        [True, True],
        "v2.0.0 already exists",
    )


def test_a_version_without_its_own_section_is_not_tagged(tmp_path: Path) -> None:
    root, commit = _repository(tmp_path, "3.0.0")

    assert release_tag.planned_release(root, commit) == "CHANGELOG.md has no section for 3.0.0"


def test_this_repository_declares_a_version_its_changelog_describes() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    version = release_tag.project_version(pyproject)

    assert release_tag.changelog_section(changelog, version) is not None
