"""Tag the version a green `main` commit declares, once.

`.github/workflows/release-tag.yml` runs this after the `tests` workflow succeeds
for a push to `main`. When the commit's `pyproject.toml` version has its own
`## [X.Y.Z]` section in `CHANGELOG.md` and no `vX.Y.Z` tag exists, it creates the
annotated tag on that commit — the changelog section and the release manifest are
its message — and, with `--push`, pushes that one tag. An existing tag is never
moved. See `docs/research/2026-09-24-a-green-main-tags-its-version.md`.

    python scripts/release_tag.py --commit <sha> [--push]
"""
from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import release_manifest

ROOT = Path(__file__).resolve().parent.parent
_PROJECT_TABLE = re.compile(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", re.M | re.S)
_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.M)
_BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


@dataclass(frozen=True)
class Release:
    tag: str
    commit: str
    message: str


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=str(root), capture_output=True, text=True, check=False
    )


def _file_at(root: Path, commit: str, path: str) -> str:
    result = _git(root, "show", f"{commit}:{path}")
    if result.returncode != 0:
        raise SystemExit(f"missing from {commit}: {path}")
    return result.stdout


def project_version(pyproject: str) -> str | None:
    """The `version` of the `[project]` table."""
    table = _PROJECT_TABLE.search(pyproject)
    if table is None:
        return None
    version = _VERSION.search(table.group(1))
    return version.group(1) if version else None


def changelog_section(changelog: str, version: str) -> str | None:
    """The body of `## [version]`, up to the next version heading."""
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S
    )
    match = pattern.search(changelog)
    if match is None:
        return None
    return match.group(1).strip() or None


def tag_exists(root: Path, tag: str) -> bool:
    return _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode == 0


def _message(root: Path, tag: str, commit: str, section: str) -> str:
    hashes = release_manifest.manifest(commit, root)
    manifest = release_manifest.markdown(tag, commit, hashes)
    return f"LLM Wiki {tag[1:]}\n\n{section}\n\n{manifest}"


def planned_release(root: Path, commit: str) -> Release | str:
    """The tag this commit should carry, or why it carries none."""
    version = project_version(_file_at(root, commit, "pyproject.toml"))
    if version is None:
        return "pyproject.toml declares no project version"
    return _release_of(root, commit, version)


def _release_of(root: Path, commit: str, version: str) -> Release | str:
    tag = f"v{version}"
    if tag_exists(root, tag):
        return f"{tag} already exists"
    section = changelog_section(_file_at(root, commit, "CHANGELOG.md"), version)
    if section is None:
        return f"CHANGELOG.md has no section for {version}"
    return Release(tag, commit, _message(root, tag, commit, section))


def _run_or_exit(root: Path, *arguments: str) -> None:
    result = _git(root, *arguments)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {arguments[0]} failed")


def publish(root: Path, release: Release, *, push: bool) -> None:
    name, email = _BOT
    identity = ("-c", f"user.name={name}", "-c", f"user.email={email}")
    _run_or_exit(root, *identity, "tag", "-a", release.tag, "-m", release.message, release.commit)
    if push:
        _run_or_exit(root, "push", "origin", f"refs/tags/{release.tag}:refs/tags/{release.tag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag the version a green main commit declares.")
    parser.add_argument("--commit", required=True, help="The commit the tests passed on")
    parser.add_argument("--push", action="store_true", help="Push the new tag to origin")
    arguments = parser.parse_args(argv)
    commit = release_manifest.commit_of(arguments.commit)
    planned = planned_release(ROOT, commit)
    if isinstance(planned, str):
        print(f"no tag: {planned}")
        return 0
    publish(ROOT, planned, push=arguments.push)
    print(f"tagged {planned.tag} at {planned.commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
