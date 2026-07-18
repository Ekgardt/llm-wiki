"""Bounded canonical snapshots of every claim-capable Markdown path."""
from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, restricted_relative_path, sha256_bytes

MAX_CLAIM_TREE_PAGES = 10_000
MAX_CLAIM_TREE_FILE_BYTES = 4 * 1024 * 1024
MAX_CLAIM_TREE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_CLAIM_TREE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_GUARDRAIL_SOURCE_FILES = 10_000
MAX_GUARDRAIL_INSPECTED_ENTRIES = 50_000
MAX_GUARDRAIL_SOURCE_DIRECTORIES = 5_000
MAX_GUARDRAIL_SOURCE_DEPTH = 12
MAX_GUARDRAIL_SOURCE_FILE_BYTES = 4 * 1024 * 1024
MAX_GUARDRAIL_SOURCE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_GUARDRAIL_SOURCE_MANIFEST_BYTES = 2 * 1024 * 1024
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


def _guardrail_source_paths(vault: Path) -> list[Path]:
    roots = []
    directory_count = 0
    for relative, recursive in (
        ("knowledge/notes", True),
        ("knowledge/feedback", False),
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
            raise PermissionError("guardrails source root must be a regular directory")
        restricted_relative_path(relative, ("knowledge/notes", "knowledge/feedback"))
        directory_count += 1
        if directory_count > MAX_GUARDRAIL_SOURCE_DIRECTORIES:
            raise ValueError("guardrails sources exceed the directory limit")
        roots.append((root, 0, recursive))

    inspected_entries = 0
    normalized: dict[str, Path] = {}
    stack = list(reversed(roots))
    while stack:
        current, depth, recursive = stack.pop()
        entries = []
        with os.scandir(current) as iterator:
            for entry in iterator:
                inspected_entries += 1
                if inspected_entries > MAX_GUARDRAIL_INSPECTED_ENTRIES:
                    raise ValueError(
                        "guardrails sources exceed the inspected entry limit"
                    )
                entries.append(entry)

        child_directories = []
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            relative = unicodedata.normalize(
                "NFC", path.relative_to(vault).as_posix()
            )
            restricted_relative_path(
                relative, ("knowledge/notes", "knowledge/feedback")
            )
            metadata = entry.stat(follow_symlinks=False)
            is_reparse = bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if entry.is_symlink() or is_reparse:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if not recursive:
                    continue
                child_depth = depth + 1
                if child_depth > MAX_GUARDRAIL_SOURCE_DEPTH:
                    raise ValueError("guardrails sources exceed the depth limit")
                directory_count += 1
                if directory_count > MAX_GUARDRAIL_SOURCE_DIRECTORIES:
                    raise ValueError("guardrails sources exceed the directory limit")
                child_directories.append((path, child_depth, recursive))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            expected_suffix = ".md" if recursive else ".json"
            if path.suffix != expected_suffix:
                continue
            if relative in normalized and normalized[relative] != path:
                raise ValueError("guardrails source path normalization collision")
            normalized[relative] = path
            if len(normalized) > MAX_GUARDRAIL_SOURCE_FILES:
                raise ValueError("guardrails sources exceed the file limit")
        stack.extend(reversed(child_directories))
    return [normalized[relative] for relative in sorted(normalized)]


def snapshot_guardrail_sources_with_content(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Return a bounded manifest and the exact source bytes it hashes."""
    vault = Path(vault).resolve(strict=True)
    discovered = _guardrail_source_paths(vault)
    entries = []
    contents: dict[str, bytes] = {}
    total = 0
    for path in discovered:
        content = read_stable_bytes(
            path,
            MAX_GUARDRAIL_SOURCE_FILE_BYTES,
            label="guardrails source",
        )
        total += len(content)
        if total > MAX_GUARDRAIL_SOURCE_TOTAL_BYTES:
            raise ValueError("guardrails sources exceed the total byte limit")
        relative = unicodedata.normalize("NFC", path.relative_to(vault).as_posix())
        contents[relative] = content
        entries.append({"path": relative, "sha256": sha256_bytes(content)})
    if discovered != _guardrail_source_paths(vault):
        raise ClaimTreeChanged("guardrails source membership changed during snapshot")
    digest = sha256_bytes(canonical_json_bytes(entries))
    manifest = {
        "schema_version": "guardrails-source-manifest/v1",
        "entries": entries,
        "source_manifest_sha256": digest,
    }
    if len(canonical_json_bytes(manifest)) > MAX_GUARDRAIL_SOURCE_MANIFEST_BYTES:
        raise ValueError("guardrails source manifest exceeds the byte limit")
    return manifest, contents


def snapshot_guardrail_sources(vault: Path) -> dict[str, object]:
    return snapshot_guardrail_sources_with_content(vault)[0]


def validate_guardrail_source_manifest(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "entries",
        "source_manifest_sha256",
    }:
        raise ValueError("guardrails source manifest fields are invalid")
    if value["schema_version"] != "guardrails-source-manifest/v1":
        raise ValueError("guardrails source manifest version is invalid")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_GUARDRAIL_SOURCE_FILES:
        raise ValueError("guardrails source manifest entries are invalid")
    paths = []
    normalized_paths = []
    normalized_entries = []
    for item in entries:
        path = item.get("path") if isinstance(item, Mapping) else None
        valid_path = isinstance(path, str) and (
            (path.startswith("knowledge/notes/") and path.endswith(".md"))
            or (path.startswith("knowledge/feedback/") and path.endswith(".json"))
        )
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "sha256"}
            or not valid_path
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in item["sha256"])
        ):
            raise ValueError("guardrails source manifest entry is invalid")
        paths.append(path)
        normalized_paths.append(unicodedata.normalize("NFC", path))
        normalized_entries.append(dict(item))
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValueError("guardrails source path normalization collision")
    if paths != normalized_paths:
        raise ValueError("guardrails source manifest paths must use NFC")
    if paths != sorted(set(paths)):
        raise ValueError("guardrails source manifest paths are not unique and sorted")
    digest = sha256_bytes(canonical_json_bytes(normalized_entries))
    if value["source_manifest_sha256"] != digest:
        raise ValueError("guardrails source manifest digest is invalid")
    result = {
        "schema_version": "guardrails-source-manifest/v1",
        "entries": normalized_entries,
        "source_manifest_sha256": digest,
    }
    if len(canonical_json_bytes(result)) > MAX_GUARDRAIL_SOURCE_MANIFEST_BYTES:
        raise ValueError("guardrails source manifest exceeds the byte limit")
    return result
