"""Bounded canonical snapshots of every claim-capable Markdown path."""
from __future__ import annotations

import stat
from collections.abc import Mapping
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, sha256_bytes

MAX_CLAIM_TREE_PAGES = 10_000
MAX_CLAIM_TREE_FILE_BYTES = 4 * 1024 * 1024
MAX_CLAIM_TREE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_CLAIM_TREE_MANIFEST_BYTES = 2 * 1024 * 1024
PROJECT_CLAIM_FILES = frozenset({"context.md", "journal.md", "state.md"})


class ClaimTreeChanged(RuntimeError):
    """The claim-capable path set changed while it was being captured."""


def _paths(vault: Path) -> list[Path]:
    pages = []
    for relative, project_only in (
        ("knowledge/notes", False),
        ("knowledge/projects", True),
    ):
        root = vault / relative
        if not root.exists():
            continue
        metadata = root.lstat()
        if (
            root.is_symlink()
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise PermissionError("claim tree root must be a regular directory")
        pages.extend(
            path
            for path in root.rglob("*.md")
            if path.is_file() and (not project_only or path.name in PROJECT_CLAIM_FILES)
        )
    if len(pages) > MAX_CLAIM_TREE_PAGES:
        raise ValueError("claim tree exceeds the page limit")
    return sorted(pages, key=lambda item: item.relative_to(vault).as_posix())


def _snapshot_claim_tree(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    vault = Path(vault).resolve(strict=True)
    discovered = _paths(vault)
    entries = []
    contents: dict[str, bytes] = {}
    total = 0
    for path in discovered:
        content = read_stable_bytes(
            path, MAX_CLAIM_TREE_FILE_BYTES, label="claim tree page"
        )
        total += len(content)
        if total > MAX_CLAIM_TREE_TOTAL_BYTES:
            raise ValueError("claim tree exceeds the total byte limit")
        relative = path.relative_to(vault).as_posix()
        contents[relative] = content
        entries.append({"path": relative, "sha256": sha256_bytes(content)})
    if [path.relative_to(vault).as_posix() for path in discovered] != [
        path.relative_to(vault).as_posix() for path in _paths(vault)
    ]:
        raise ClaimTreeChanged("claim tree membership changed during snapshot")
    generation = sha256_bytes(canonical_json_bytes(entries))
    manifest = {
        "schema_version": "claim-tree-manifest/v1",
        "entries": entries,
        "absence_generation": generation,
    }
    if len(canonical_json_bytes(manifest)) > MAX_CLAIM_TREE_MANIFEST_BYTES:
        raise ValueError("claim tree manifest exceeds the byte limit")
    return manifest, contents


def snapshot_claim_tree(vault: Path) -> dict[str, object]:
    return _snapshot_claim_tree(vault)[0]


def snapshot_claim_tree_with_content(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Return one bounded manifest and the exact bytes hashed into it."""
    return _snapshot_claim_tree(vault)


def validate_claim_tree_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "entries",
        "absence_generation",
    }:
        raise ValueError("claim tree manifest fields are invalid")
    if value["schema_version"] != "claim-tree-manifest/v1":
        raise ValueError("claim tree manifest version is invalid")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_CLAIM_TREE_PAGES:
        raise ValueError("claim tree manifest entries are invalid")
    paths = []
    normalized_entries = []
    for item in entries:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or not item["path"].endswith(".md")
            or not item["path"].startswith(
                ("knowledge/notes/", "knowledge/projects/")
            )
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["sha256"])
        ):
            raise ValueError("claim tree manifest entry is invalid")
        paths.append(item["path"])
        normalized_entries.append(dict(item))
    if paths != sorted(set(paths)):
        raise ValueError("claim tree manifest paths are not unique and sorted")
    generation = sha256_bytes(canonical_json_bytes(normalized_entries))
    if value["absence_generation"] != generation:
        raise ValueError("claim tree manifest generation is invalid")
    result = {
        "schema_version": "claim-tree-manifest/v1",
        "entries": normalized_entries,
        "absence_generation": generation,
    }
    if len(canonical_json_bytes(result)) > MAX_CLAIM_TREE_MANIFEST_BYTES:
        raise ValueError("claim tree manifest exceeds the byte limit")
    return result
