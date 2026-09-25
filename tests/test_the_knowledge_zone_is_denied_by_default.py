"""Nothing new under `knowledge/` can be staged by accident, and results name no machine.

The runtime writes `knowledge/guardrails.md` from private sessions and no rule ignored it.
Research: `docs/research/2026-09-17-the-knowledge-zone-is-denied-by-default.md`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUBLISHED = {
    "knowledge/README.md",
    "knowledge/index.md",
    "knowledge/log.md",
    "knowledge/daily/README.md",
    "knowledge/inbox/README.md",
    "knowledge/notes/README.md",
    "knowledge/projects/README.md",
    "knowledge/projects/_template/state.md",
    "knowledge/raw/README.md",
}
HOME_MARKERS = ("/home/", "/Users/", "C:\\\\Users\\\\")


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True, text=True, check=False)


needs_git = pytest.mark.skipif(_git("rev-parse", "--git-dir").returncode != 0, reason="not a git checkout")


@needs_git
@pytest.mark.parametrize(
    "path",
    [
        "knowledge/guardrails.md",
        "knowledge/anything-new.md",
        "knowledge/new-directory/page.md",
        "knowledge/daily/2026-01-01.json",
        "knowledge/daily/receipts/r.json",
        "knowledge/notes/private-page.md",
    ],
)
def test_a_new_file_in_the_knowledge_zone_is_ignored(path: str) -> None:
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 0


@needs_git
@pytest.mark.parametrize("path", sorted(PUBLISHED))
def test_what_the_repository_ships_is_not_ignored(path: str) -> None:
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 1


@needs_git
def test_every_tracked_knowledge_path_is_on_the_published_list() -> None:
    tracked = set(_git("ls-files", "knowledge").stdout.split())

    assert tracked - PUBLISHED == set()


@needs_git
def test_no_tracked_benchmark_result_names_a_home_directory() -> None:
    pattern = "|".join(marker.replace("/", "\\/") for marker in HOME_MARKERS)

    found = _git("grep", "-l", "-E", pattern, "--", "benchmark/*.json").stdout.split()

    assert found == [], "replace the checkout path with <vault> before committing a result"
