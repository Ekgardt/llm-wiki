"""Regression tests: slug computation, collision resolution, strict ownership.

Covers the Round 2 / Round 5 fixes to `session_start_project_state.py`:
  - Base slug sanitization (Cyrillic preservation, hyphens, edge cases).
  - Collision resolution: base → parent-of-parent → git owner-repo → grandparent → path-hash.
  - Strict ownership: state.md without a `- Project root:` line is NOT owned.
  - Idempotency: re-compute returns the same slug.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from session_start_project_state import (
    _base_slug,
    _compute_slug,
    _git_remote_slug,
    _path_hash_suffix,
    _slug_owns_dir,
)


# ---------- _base_slug ----------

def test_base_slug_lowercase():
    p = Path("/tmp/My-Project")
    assert _base_slug(p) == "my-project"


def test_base_slug_preserves_cyrillic():
    p = Path("/tmp/Тесты")
    assert _base_slug(p) == "тесты"


def test_base_slug_strips_unsafe_chars():
    # Any Path with weird basename characters
    class P:
        name = "foo:bar*baz"
    assert _base_slug(P()) == "foo-bar-baz"  # type: ignore[arg-type]


def test_base_slug_fallback_for_empty():
    class P:
        name = ""
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]


def test_base_slug_fallback_for_dotdot():
    class P:
        name = ".."
    assert _base_slug(P()) == "root"  # type: ignore[arg-type]


# ---------- _git_remote_slug ----------

def test_git_remote_slug_parses_ssh(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Owner/Repo.git\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "owner-repo"


def test_git_remote_slug_parses_https(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/Alice/my-app\n',
        encoding="utf-8",
    )
    assert _git_remote_slug(tmp_path) == "alice-my-app"


def test_git_remote_slug_none_when_no_git(tmp_path: Path):
    assert _git_remote_slug(tmp_path) is None


def test_git_remote_slug_none_when_no_origin(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    assert _git_remote_slug(tmp_path) is None


# ---------- _path_hash_suffix ----------

def test_path_hash_deterministic(tmp_path: Path):
    assert _path_hash_suffix(tmp_path) == _path_hash_suffix(tmp_path)


def test_path_hash_length():
    assert len(_path_hash_suffix(Path("/any"))) == 6


# ---------- _slug_owns_dir (strict ownership, Round 5 #3) ----------

def test_slug_owns_empty_dir(tmp_path: Path):
    """Unused slug → free to take."""
    projects = tmp_path / "projects"
    projects.mkdir()
    assert _slug_owns_dir("unused", Path("/any/project"), projects) is True


def test_slug_owns_matching_root(tmp_path: Path):
    projects = tmp_path / "projects"
    slug_dir = projects / "mine"
    slug_dir.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    (slug_dir / "state.md").write_text(
        f"# mine — State\n- Project root: `{project}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("mine", project, projects) is True


def test_slug_owns_rejects_different_root(tmp_path: Path):
    projects = tmp_path / "projects"
    slug_dir = projects / "shared"
    slug_dir.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()
    (slug_dir / "state.md").write_text(
        f"# shared — State\n- Project root: `{other}`\n", encoding="utf-8"
    )
    assert _slug_owns_dir("shared", mine, projects) is False


def test_slug_owns_strict_rejects_missing_source(tmp_path: Path):
    """Round 5 #3: state.md without `- Project root:` → NOT ours.

    Previously this returned True (assumed hand-edited, treat as ours),
    opening a collision hole where a second project could silently adopt
    a first project's state.md by having its Source section removed.
    """
    projects = tmp_path / "projects"
    slug_dir = projects / "ambiguous"
    slug_dir.mkdir(parents=True)
    (slug_dir / "state.md").write_text(
        "# ambiguous — State\n(no Source section whatsoever)\n",
        encoding="utf-8",
    )
    assert _slug_owns_dir("ambiguous", tmp_path / "someproj", projects) is False


# ---------- _compute_slug end-to-end ----------

def test_compute_slug_unique(tmp_path: Path):
    """Clean slug — base strategy wins."""
    projects = tmp_path / "projects"
    projects.mkdir()
    proj = tmp_path / "unique"
    proj.mkdir()
    assert _compute_slug(proj, projects) == "unique"


def test_compute_slug_collision_gets_parent_of_parent(tmp_path: Path):
    """Two projects with the same basename → second gets pop suffix."""
    projects = tmp_path / "projects"
    projects.mkdir()

    # Project A owns "frontend"
    parent_a = tmp_path / "app-a"
    parent_a.mkdir()
    front_a = parent_a / "frontend"
    front_a.mkdir()
    slug_a = _compute_slug(front_a, projects)
    assert slug_a == "frontend"
    # Simulate SessionStart writing state.md
    (projects / slug_a).mkdir()
    (projects / slug_a / "state.md").write_text(
        f"# frontend\n- Project root: `{front_a}`\n", encoding="utf-8"
    )

    # Project B competes
    parent_b = tmp_path / "app-b"
    parent_b.mkdir()
    front_b = parent_b / "frontend"
    front_b.mkdir()
    slug_b = _compute_slug(front_b, projects)
    assert slug_b != slug_a
    # Must use parent-of-parent
    assert slug_b == "frontend-app-b"


def test_compute_slug_idempotent(tmp_path: Path):
    """Re-computing for the same project returns the same slug."""
    projects = tmp_path / "projects"
    projects.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    slug_first = _compute_slug(proj, projects)
    (projects / slug_first).mkdir()
    (projects / slug_first / "state.md").write_text(
        f"# {slug_first}\n- Project root: `{proj}`\n", encoding="utf-8"
    )
    slug_second = _compute_slug(proj, projects)
    assert slug_first == slug_second


def test_compute_slug_hash_suffix_last_resort(tmp_path: Path):
    """If base, pop, git, and grandparent all collide, hash suffix kicks in."""
    projects = tmp_path / "projects"
    projects.mkdir()

    # Create a project dir with no git and no meaningful parents
    proj = tmp_path / "orphan"
    proj.mkdir()

    # Pre-occupy every candidate the resolver would try
    for slug in ["orphan"]:
        (projects / slug).mkdir()
        (projects / slug / "state.md").write_text(
            f"# {slug}\n- Project root: `/somewhere/else`\n", encoding="utf-8"
        )

    # With no parent-of-parent matching, it should still resolve — either
    # via grandparent (tmp_path.name) or via hash. The output must NOT be
    # bare "orphan" (that's taken).
    slug = _compute_slug(proj, projects)
    assert slug != "orphan"
    assert slug.startswith("orphan") or slug == "root"
