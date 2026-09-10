"""Bounded canonical snapshots of every claim-capable Markdown path."""
from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path

from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, restricted_relative_path, sha256_bytes

MAX_CLAIM_TREE_PAGES = 10_000
# Eight megabytes: the same ceiling `project_journal.MAX_JOURNAL_BYTES` allows
# a journal, so a page the journal accepts is never one the claim tree refuses.
# Measured 2026-09-09: a 4.2 MB journal failed every compile since 09-07.
MAX_CLAIM_TREE_FILE_BYTES = 8 * 1024 * 1024
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

_CLAIM_TREE_FIELDS = frozenset({"schema_version", "entries", "absence_generation"})
_GUARDRAIL_FIELDS = frozenset({"schema_version", "entries", "source_manifest_sha256"})
_ENTRY_FIELDS = frozenset({"path", "sha256"})
_GUARDRAIL_ROOTS = ("knowledge/notes", "knowledge/feedback")
_HEX_DIGITS = "0123456789abcdef"


class ClaimTreeChanged(RuntimeError):
    """The claim-capable path set changed while it was being captured."""


# --- shared helpers ---------------------------------------------------------


def _is_regular_directory(root: Path, metadata: os.stat_result) -> bool:
    if root.is_symlink():
        return False
    if getattr(metadata, "st_file_attributes", 0) & 0x400:
        return False
    return stat.S_ISDIR(metadata.st_mode)


def _require_regular_directory(root: Path, message: str) -> None:
    metadata = root.lstat()
    if _is_regular_directory(root, metadata):
        return
    raise PermissionError(message)


def _require_manifest_within(manifest: Mapping[str, object], limit: int, message: str) -> None:
    if len(canonical_json_bytes(manifest)) > limit:
        raise ValueError(message)


def _require_manifest_shape(value: object, fields: frozenset[str], message: str) -> None:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(message)


def _require_entry_list(entries: object, limit: int, message: str) -> None:
    if not isinstance(entries, list) or len(entries) > limit:
        raise ValueError(message)


def _require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def _require_sorted_unique(paths: list[str], message: str) -> None:
    if paths != sorted(set(paths)):
        raise ValueError(message)


def _is_hex_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 64:
        return False
    return not any(character not in _HEX_DIGITS for character in value)


def _is_manifest_entry(item: object, path_ok: Callable[[object], bool]) -> bool:
    if not isinstance(item, Mapping):
        return False
    if set(item) != _ENTRY_FIELDS:
        return False
    return path_ok(item["path"]) and _is_hex_digest(item["sha256"])


def _hash_sources(
    vault: Path,
    discovered: list[Path],
    *,
    file_limit: int,
    total_limit: int,
    label: str,
    total_message: str,
    relative_of: Callable[[Path, Path], str],
) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    entries: list[dict[str, str]] = []
    contents: dict[str, bytes] = {}
    total = 0
    for path in discovered:
        content = read_stable_bytes(path, file_limit, label=label)
        total += len(content)
        if total > total_limit:
            raise ValueError(total_message)
        relative = relative_of(vault, path)
        contents[relative] = content
        entries.append({"path": relative, "sha256": sha256_bytes(content)})
    return entries, contents


# --- claim tree -------------------------------------------------------------


def _claim_relative(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def _relative_names(vault: Path, paths: list[Path]) -> list[str]:
    return [_claim_relative(vault, path) for path in paths]


def _is_claim_page(path: Path, project_only: bool) -> bool:
    if not path.is_file():
        return False
    if not project_only:
        return True
    return path.name in PROJECT_CLAIM_FILES


def _claim_pages_under(root: Path, project_only: bool) -> list[Path]:
    return [path for path in root.rglob("*.md") if _is_claim_page(path, project_only)]


def _paths(vault: Path) -> list[Path]:
    pages = []
    for relative, project_only in (
        ("knowledge/notes", False),
        ("knowledge/projects", True),
    ):
        root = vault / relative
        if not root.exists():
            continue
        _require_regular_directory(root, "claim tree root must be a regular directory")
        pages.extend(_claim_pages_under(root, project_only))
    if len(pages) > MAX_CLAIM_TREE_PAGES:
        raise ValueError("claim tree exceeds the page limit")
    return sorted(pages, key=lambda item: item.relative_to(vault).as_posix())


def _snapshot_claim_tree(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    vault = Path(vault).resolve(strict=True)
    discovered = _paths(vault)
    entries, contents = _hash_sources(
        vault,
        discovered,
        file_limit=MAX_CLAIM_TREE_FILE_BYTES,
        total_limit=MAX_CLAIM_TREE_TOTAL_BYTES,
        label="claim tree page",
        total_message="claim tree exceeds the total byte limit",
        relative_of=_claim_relative,
    )
    if _relative_names(vault, discovered) != _relative_names(vault, _paths(vault)):
        raise ClaimTreeChanged("claim tree membership changed during snapshot")
    generation = sha256_bytes(canonical_json_bytes(entries))
    manifest = {
        "schema_version": "claim-tree-manifest/v1",
        "entries": entries,
        "absence_generation": generation,
    }
    _require_manifest_within(
        manifest, MAX_CLAIM_TREE_MANIFEST_BYTES, "claim tree manifest exceeds the byte limit"
    )
    return manifest, contents


def snapshot_claim_tree(vault: Path) -> dict[str, object]:
    return _snapshot_claim_tree(vault)[0]


def snapshot_claim_tree_with_content(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Return one bounded manifest and the exact bytes hashed into it."""
    return _snapshot_claim_tree(vault)


def _is_claim_entry_path(path: object) -> bool:
    if not isinstance(path, str):
        return False
    if not path.endswith(".md"):
        return False
    return path.startswith(("knowledge/notes/", "knowledge/projects/"))


def _claim_tree_entries(entries: list[object]) -> tuple[list[str], list[dict[str, object]]]:
    paths = []
    normalized_entries = []
    for item in entries:
        if not _is_manifest_entry(item, _is_claim_entry_path):
            raise ValueError("claim tree manifest entry is invalid")
        paths.append(item["path"])
        normalized_entries.append(dict(item))
    return paths, normalized_entries


def validate_claim_tree_manifest(value: object) -> dict[str, object]:
    _require_manifest_shape(
        value, _CLAIM_TREE_FIELDS, "claim tree manifest fields are invalid"
    )
    _require_equal(
        value["schema_version"],
        "claim-tree-manifest/v1",
        "claim tree manifest version is invalid",
    )
    entries = value["entries"]
    _require_entry_list(
        entries, MAX_CLAIM_TREE_PAGES, "claim tree manifest entries are invalid"
    )
    paths, normalized_entries = _claim_tree_entries(entries)
    _require_sorted_unique(paths, "claim tree manifest paths are not unique and sorted")
    generation = sha256_bytes(canonical_json_bytes(normalized_entries))
    _require_equal(
        value["absence_generation"],
        generation,
        "claim tree manifest generation is invalid",
    )
    result = {
        "schema_version": "claim-tree-manifest/v1",
        "entries": normalized_entries,
        "absence_generation": generation,
    }
    _require_manifest_within(
        result, MAX_CLAIM_TREE_MANIFEST_BYTES, "claim tree manifest exceeds the byte limit"
    )
    return result


# --- guardrail sources ------------------------------------------------------


def _guardrail_relative(vault: Path, path: Path) -> str:
    return unicodedata.normalize("NFC", path.relative_to(vault).as_posix())


def _is_link_like(entry: os.DirEntry, metadata: os.stat_result) -> bool:
    is_reparse = bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    return entry.is_symlink() or is_reparse


def _expected_source_suffix(recursive: bool) -> str:
    if recursive:
        return ".md"
    return ".json"


class _GuardrailSourceWalk:
    """Bounded, symlink-refusing walk over the guardrail source roots."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.directory_count = 0
        self.inspected_entries = 0
        self.normalized: dict[str, Path] = {}
        self.stack: list[tuple[Path, int, bool]] = []

    def _count_directory(self) -> None:
        self.directory_count += 1
        if self.directory_count > MAX_GUARDRAIL_SOURCE_DIRECTORIES:
            raise ValueError("guardrails sources exceed the directory limit")

    def add_roots(self) -> None:
        roots = []
        for relative, recursive in (
            ("knowledge/notes", True),
            ("knowledge/feedback", False),
        ):
            root = self.vault / relative
            if not root.exists():
                continue
            _require_regular_directory(
                root, "guardrails source root must be a regular directory"
            )
            restricted_relative_path(relative, _GUARDRAIL_ROOTS)
            self._count_directory()
            roots.append((root, 0, recursive))
        self.stack = list(reversed(roots))

    def _scan(self, current: Path) -> list[os.DirEntry]:
        entries = []
        with os.scandir(current) as iterator:
            for entry in iterator:
                self.inspected_entries += 1
                if self.inspected_entries > MAX_GUARDRAIL_INSPECTED_ENTRIES:
                    raise ValueError(
                        "guardrails sources exceed the inspected entry limit"
                    )
                entries.append(entry)
        return entries

    def _visit_directory(
        self, path: Path, depth: int, recursive: bool
    ) -> tuple[Path, int, bool] | None:
        if not recursive:
            return None
        child_depth = depth + 1
        if child_depth > MAX_GUARDRAIL_SOURCE_DEPTH:
            raise ValueError("guardrails sources exceed the depth limit")
        self._count_directory()
        return (path, child_depth, recursive)

    def _record_file(self, relative: str, path: Path) -> None:
        if relative in self.normalized and self.normalized[relative] != path:
            raise ValueError("guardrails source path normalization collision")
        self.normalized[relative] = path
        if len(self.normalized) > MAX_GUARDRAIL_SOURCE_FILES:
            raise ValueError("guardrails sources exceed the file limit")

    def _visit_file(
        self, path: Path, relative: str, metadata: os.stat_result, recursive: bool
    ) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            return
        if path.suffix != _expected_source_suffix(recursive):
            return
        self._record_file(relative, path)

    def _visit_entry(
        self, entry: os.DirEntry, depth: int, recursive: bool
    ) -> tuple[Path, int, bool] | None:
        path = Path(entry.path)
        relative = _guardrail_relative(self.vault, path)
        restricted_relative_path(relative, _GUARDRAIL_ROOTS)
        metadata = entry.stat(follow_symlinks=False)
        if _is_link_like(entry, metadata):
            return None
        if stat.S_ISDIR(metadata.st_mode):
            return self._visit_directory(path, depth, recursive)
        self._visit_file(path, relative, metadata, recursive)
        return None

    def _visit_children(
        self, current: Path, depth: int, recursive: bool
    ) -> list[tuple[Path, int, bool]]:
        child_directories = []
        for entry in sorted(self._scan(current), key=lambda item: item.name):
            child = self._visit_entry(entry, depth, recursive)
            if child is not None:
                child_directories.append(child)
        return child_directories

    def walk(self) -> None:
        while self.stack:
            current, depth, recursive = self.stack.pop()
            child_directories = self._visit_children(current, depth, recursive)
            self.stack.extend(reversed(child_directories))

    def paths(self) -> list[Path]:
        return [self.normalized[relative] for relative in sorted(self.normalized)]


def _guardrail_source_paths(vault: Path) -> list[Path]:
    walk = _GuardrailSourceWalk(vault)
    walk.add_roots()
    walk.walk()
    return walk.paths()


def snapshot_guardrail_sources_with_content(
    vault: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Return a bounded manifest and the exact source bytes it hashes."""
    vault = Path(vault).resolve(strict=True)
    discovered = _guardrail_source_paths(vault)
    entries, contents = _hash_sources(
        vault,
        discovered,
        file_limit=MAX_GUARDRAIL_SOURCE_FILE_BYTES,
        total_limit=MAX_GUARDRAIL_SOURCE_TOTAL_BYTES,
        label="guardrails source",
        total_message="guardrails sources exceed the total byte limit",
        relative_of=_guardrail_relative,
    )
    if discovered != _guardrail_source_paths(vault):
        raise ClaimTreeChanged("guardrails source membership changed during snapshot")
    digest = sha256_bytes(canonical_json_bytes(entries))
    manifest = {
        "schema_version": "guardrails-source-manifest/v1",
        "entries": entries,
        "source_manifest_sha256": digest,
    }
    _require_manifest_within(
        manifest,
        MAX_GUARDRAIL_SOURCE_MANIFEST_BYTES,
        "guardrails source manifest exceeds the byte limit",
    )
    return manifest, contents


def snapshot_guardrail_sources(vault: Path) -> dict[str, object]:
    return snapshot_guardrail_sources_with_content(vault)[0]


def _is_notes_page_path(path: str) -> bool:
    return path.startswith("knowledge/notes/") and path.endswith(".md")


def _is_feedback_record_path(path: str) -> bool:
    return path.startswith("knowledge/feedback/") and path.endswith(".json")


def _is_guardrail_entry_path(path: object) -> bool:
    if not isinstance(path, str):
        return False
    return _is_notes_page_path(path) or _is_feedback_record_path(path)


def _guardrail_manifest_entries(
    entries: list[object],
) -> tuple[list[str], list[str], list[dict[str, object]]]:
    paths = []
    normalized_paths = []
    normalized_entries = []
    for item in entries:
        if not _is_manifest_entry(item, _is_guardrail_entry_path):
            raise ValueError("guardrails source manifest entry is invalid")
        path = item["path"]
        paths.append(path)
        normalized_paths.append(unicodedata.normalize("NFC", path))
        normalized_entries.append(dict(item))
    return paths, normalized_paths, normalized_entries


def _require_nfc_paths(paths: list[str], normalized_paths: list[str]) -> None:
    if len(normalized_paths) != len(set(normalized_paths)):
        raise ValueError("guardrails source path normalization collision")
    if paths != normalized_paths:
        raise ValueError("guardrails source manifest paths must use NFC")


def _require_guardrail_paths(paths: list[str], normalized_paths: list[str]) -> None:
    _require_nfc_paths(paths, normalized_paths)
    _require_sorted_unique(
        paths, "guardrails source manifest paths are not unique and sorted"
    )


def validate_guardrail_source_manifest(value: object) -> dict[str, object]:
    _require_manifest_shape(
        value, _GUARDRAIL_FIELDS, "guardrails source manifest fields are invalid"
    )
    _require_equal(
        value["schema_version"],
        "guardrails-source-manifest/v1",
        "guardrails source manifest version is invalid",
    )
    entries = value["entries"]
    _require_entry_list(
        entries,
        MAX_GUARDRAIL_SOURCE_FILES,
        "guardrails source manifest entries are invalid",
    )
    paths, normalized_paths, normalized_entries = _guardrail_manifest_entries(entries)
    _require_guardrail_paths(paths, normalized_paths)
    digest = sha256_bytes(canonical_json_bytes(normalized_entries))
    _require_equal(
        value["source_manifest_sha256"],
        digest,
        "guardrails source manifest digest is invalid",
    )
    result = {
        "schema_version": "guardrails-source-manifest/v1",
        "entries": normalized_entries,
        "source_manifest_sha256": digest,
    }
    _require_manifest_within(
        result,
        MAX_GUARDRAIL_SOURCE_MANIFEST_BYTES,
        "guardrails source manifest exceeds the byte limit",
    )
    return result
