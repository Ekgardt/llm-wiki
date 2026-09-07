"""A path rule cannot catch private content written inside a public page.

`.gitignore` denies every knowledge directory and names 91 published pages by
hand, so a *new* page is private by default. That half works. What it cannot do
is notice private material written into a page that is already published — and
on 2026-09-02 that is exactly what had happened: the name of another of the
owner's projects appeared twice in a published decision page and once in the
changelog, describing whose queue had jammed.

The vault knows the names of the owner's other projects: they are the
directories under `knowledge/projects/`. Any tracked page naming one of them,
or carrying a home path or the owner's address, is a leak that no path rule
would ever see.

This test fails closed. It reads what git actually tracks, not what the
`.gitignore` intends.

See `docs/research/2026-09-02-what-belongs-in-the-repository-and-what-the-undo-trail-should-be.md`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Slugs that belong to this product and appear in published pages on purpose,
# plus the short generic ones a project directory can carry.
OWN_SLUGS = frozenset(
    {"llm-wiki", "main", "user", "tmp", "docs", "_template", "checkout-claude"}
)

# A private path on the machine, and the owner's address. Neither belongs in a
# page anyone can clone.
PRIVATE_PATTERNS = (
    re.compile(r"/home/[a-z0-9_-]+/", re.IGNORECASE),
    re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.IGNORECASE),
)

# Addresses that are the project's own public identity rather than the owner's.
PUBLIC_ADDRESSES = frozenset({"noreply@anthropic.com"})


def _tracked_knowledge() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "knowledge/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in listing.splitlines() if line.endswith(".md")]


def _is_other_project(entry: Path) -> bool:
    """A directory that names one of the owner's other projects, not this one."""
    if not entry.is_dir():
        return False
    return entry.name not in OWN_SLUGS and len(entry.name) > 4


def _other_project_slugs() -> set[str]:
    projects = ROOT / "knowledge" / "projects"
    if not projects.is_dir():
        return set()
    return {entry.name for entry in projects.iterdir() if _is_other_project(entry)}


def _named_slugs(text: str, slugs: set[str]) -> set[str]:
    return {slug for slug in slugs if re.search(rf"\b{re.escape(slug)}\b", text)}


def _private_strings(text: str) -> set[str]:
    found: set[str] = set()
    for pattern in PRIVATE_PATTERNS:
        found.update(
            match for match in pattern.findall(text) if match not in PUBLIC_ADDRESSES
        )
    return found


def test_no_tracked_page_names_another_project() -> None:
    """The leak that happened: a neighbouring project named in a public page."""
    slugs = _other_project_slugs()
    offenders: dict[str, set[str]] = {}
    for path in _tracked_knowledge():
        named = _named_slugs(path.read_text(encoding="utf-8"), slugs)
        if named:
            offenders[str(path.relative_to(ROOT))] = named

    assert not offenders, f"published pages name other projects: {offenders}"


def test_no_tracked_page_carries_a_home_path_or_an_address() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _tracked_knowledge():
        found = _private_strings(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(ROOT))] = found

    assert not offenders, f"published pages carry private strings: {offenders}"


def test_the_check_reads_git_rather_than_the_ignore_file() -> None:
    """An intention is not a fact; only what git tracks can leak."""
    tracked = _tracked_knowledge()

    assert tracked, "expected published knowledge pages to exist"
    assert all(path.is_file() for path in tracked)


def test_the_vault_knows_which_projects_are_not_its_own() -> None:
    """Without that list the first check is vacuous, so say when it is.

    A clean checkout has no private project directories — they are gitignored,
    which is the whole point — so this can only be asserted where they exist.
    On CI it skips and says so rather than failing; on the owner's vault it is
    the guard against a silent pass. It failed on every platform in CI until
    2026-09-05 for exactly that reason, which is a test that did not know where
    it was.
    """
    if not _other_project_slugs():
        pytest.skip("clean checkout: no private projects to name")

    assert _other_project_slugs()


def test_a_planted_slug_would_be_caught() -> None:
    """The guard must fail on the thing it exists to catch.

    Planted rather than real, so this runs in a clean checkout too: what is
    under test is the matcher, not the contents of one machine.
    """
    slugs = {"someone-elses-project", "another-private-thing"}

    found = _named_slugs("the queue for someone-elses-project had jammed", slugs)

    assert found == {"someone-elses-project"}


def test_a_slug_inside_a_longer_word_is_not_a_match() -> None:
    """Word boundaries, or every page mentioning `api` names a project."""
    assert _named_slugs("the rapid queue", {"api"}) == set()


def test_a_planted_home_path_would_be_caught() -> None:
    assert _private_strings("see /home/someone/vault/run") == {"/home/someone/"}


def test_the_projects_own_address_is_not_a_leak() -> None:
    """Co-authorship lines are public identity, not the owner's address."""
    assert _private_strings("Co-Authored-By: Claude <noreply@anthropic.com>") == set()
