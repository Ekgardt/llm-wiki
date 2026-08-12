"""Shared definitions of editorial/metadata page sets across the vault.

Multiple scripts need to distinguish **editorial metadata** pages from
**curated content** pages:

- `lint_memory.py` exempts editorial pages from orphan, sparse, and
  backlink checks.
- `lookup_mode.py` excludes editorial pages from the curated-page
  counter that chooses the retrieval tier.

Before this module these lived as duplicate literals, drifting over
time. Centralizing them here keeps the two callers in sync and gives
new scripts a single import point.

## Conventions

`EDITORIAL_NAMES` — filenames whose presence in *any* path under the
vault marks the page as editorial. Matching is by basename, case-sensitive.

`BACKLINK_EXEMPT_NAMES` — pages that legitimately don't require inbound
backlinks from their callers (workflows and high-level entry points).

`BROKEN_LINK_SKIP_NAMES` — pages whose prose frequently contains literal
`[[...]]` that aren't real wikilinks (docs with bracket placeholders,
append-only changelogs with historical references to renamed pages).

`editorial_parents_to_skip()` — directories to skip wholesale when
enumerating content (e.g. `knowledge/projects/_template/` is a skeleton).

## What NOT to put here

Purely script-internal helpers (path resolution, state I/O) stay in
`memory_state.py`. This module is editorial policy only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from memory_state import (
    _is_unicode_noncharacter,
    bind_atomic_writes_to_directory,
    bounded_path_inventory,
    parse_frontmatter_scalar,
    parse_project_scope,
)

MAX_ACTIVE_NOTE_ENTRIES = 10_000
MAX_ACTIVE_NOTE_BYTES = 64 * 1024
MAX_ACTIVE_NOTE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DUPLICATE_DIAGNOSTICS = 100
MAX_DIAGNOSTIC_SHADOWS = 16
MAX_DIAGNOSTIC_TEXT_CHARS = 512
READ_CHUNK_BYTES = 64 * 1024
UNSAFE_MARKDOWN_PATH_DELIMITERS = frozenset("`#|^[]")

AUTHORITY_RANKS = {
    "inferred": 1,
    "ai-derived": 2,
    "web": 3,
    "user": 4,
}
CONFIDENCE_RANKS = {
    "low": 1,
    "medium": 2,
    "high": 3,
}

_H1_OPEN_RE = re.compile(r"^ {0,3}#(?:[ \t]+|$)")
_H1_RE = re.compile(r"^ {0,3}#(?:[ \t]+(.*?))?[ \t]*$")
ACTIVE_NOTE_GENERATION_VERSION = 1
_EMPTY_GENERATION_SHA256 = hashlib.sha256(b"[]").hexdigest()


@dataclass(frozen=True)
class ActiveNoteMetadata:
    page_type: str
    project: str
    authority: str
    confidence: str


@dataclass(frozen=True)
class ActiveNoteFileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    file_attributes: int
    nlink: int


@dataclass(frozen=True)
class ActiveNote:
    path: Path
    relative_path: str
    content: str
    source_bytes: bytes
    content_sha256: str
    file_identity: ActiveNoteFileIdentity
    title: str
    slug: str
    page_type: str
    project: str
    authority: str
    confidence: str
    authority_rank: int
    confidence_rank: int
    is_flat: bool


@dataclass(frozen=True)
class DuplicateDiagnostic:
    identity: str
    canonical: str
    shadows: tuple[str, ...]
    shadow_count: int
    shadows_truncated: bool
    kind: str = "duplicate"


@dataclass(frozen=True)
class ActiveNoteGeneration:
    version: int
    inventory_sha256: str
    canonical_sha256: str


EMPTY_ACTIVE_NOTE_GENERATION = ActiveNoteGeneration(
    version=ACTIVE_NOTE_GENERATION_VERSION,
    inventory_sha256=_EMPTY_GENERATION_SHA256,
    canonical_sha256=_EMPTY_GENERATION_SHA256,
)


@dataclass(frozen=True)
class ActiveNoteSelection:
    notes: tuple[ActiveNote, ...]
    diagnostics: tuple[DuplicateDiagnostic, ...]
    diagnostics_truncated: bool = False
    candidate_identities: frozenset[str] = frozenset()
    generation: ActiveNoteGeneration = EMPTY_ACTIVE_NOTE_GENERATION

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(note.path for note in self.notes)


@dataclass(frozen=True)
class BoundedNoteSnapshot:
    content: str
    source_bytes: bytes
    content_sha256: str
    file_identity: ActiveNoteFileIdentity
    byte_size: int


def _is_reparse_point(metadata) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _active_note_file_identity(metadata) -> ActiveNoteFileIdentity:
    return ActiveNoteFileIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=int(metadata.st_size),
        mtime_ns=int(metadata.st_mtime_ns),
        ctime_ns=int(metadata.st_ctime_ns),
        mode=int(metadata.st_mode),
        file_attributes=int(getattr(metadata, "st_file_attributes", 0)),
        nlink=int(metadata.st_nlink),
    )


def _file_identity_record(identity: ActiveNoteFileIdentity) -> list[int]:
    return [
        identity.device,
        identity.inode,
        identity.size,
        identity.mtime_ns,
        identity.ctime_ns,
        identity.mode,
        identity.file_attributes,
        identity.nlink,
    ]


def _path_matches_open_file(path_metadata, opened_metadata) -> bool:
    return (
        os.path.samestat(path_metadata, opened_metadata)
        and path_metadata.st_size == opened_metadata.st_size
        and path_metadata.st_mtime_ns == opened_metadata.st_mtime_ns
        and stat.S_IMODE(path_metadata.st_mode) == stat.S_IMODE(opened_metadata.st_mode)
        and getattr(path_metadata, "st_file_attributes", 0)
        == getattr(opened_metadata, "st_file_attributes", 0)
        and path_metadata.st_nlink == opened_metadata.st_nlink
    )


def _generation_sha256(records: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        records,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _open_windows_note(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    path_text = os.path.abspath(path)
    if not path_text.startswith("\\\\?\\"):
        path_text = (
            "\\\\?\\UNC\\" + path_text[2:]
            if path_text.startswith("\\\\")
            else "\\\\?\\" + path_text
        )
    handle = create_file(
        path_text,
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        error = ctypes.get_last_error()
        raise OSError(error, f"CreateFileW failed with Windows error {error}")
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _bound_metadata(path: Path, bound):
    if bound.descriptor is None:
        return path.lstat()
    return os.stat(path.name, dir_fd=bound.descriptor, follow_symlinks=False)


def read_bounded_note_snapshot(
    path: Path,
    max_bytes: int = MAX_ACTIVE_NOTE_BYTES,
) -> BoundedNoteSnapshot:
    """Snapshot one regular UTF-8 note through a stable no-follow binding."""
    if max_bytes < 0:
        raise OSError("active note byte limit is invalid")
    target = Path(path)
    with bind_atomic_writes_to_directory(target.parent) as bound:
        bound.validate_path()
        metadata = _bound_metadata(target, bound)
        if metadata.st_nlink != 1:
            raise OSError("active note is hard-linked")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse_point(metadata)
            or metadata.st_size > max_bytes
        ):
            raise OSError("active note is unsafe or oversized")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = (
            _open_windows_note(target)
            if os.name == "nt"
            else os.open(target.name, flags, dir_fd=bound.descriptor)
            if bound.descriptor is not None
            else os.open(target, flags)
        )
        try:
            opened = os.fstat(descriptor)
            if opened.st_nlink != 1:
                raise OSError("active note is hard-linked")
            if (
                not _path_matches_open_file(metadata, opened)
                or not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
                or opened.st_size > max_bytes
            ):
                raise OSError("active note changed while opening")
            chunks: list[bytes] = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            opened_after = os.fstat(descriptor)
            current = _bound_metadata(target, bound)
            bound.validate_path()
        finally:
            os.close(descriptor)
        if opened_after.st_nlink != 1 or current.st_nlink != 1:
            raise OSError("active note is hard-linked")
        if (
            remaining != 0
            or len(raw) != opened.st_size
            or not os.path.samestat(opened, opened_after)
            or not _path_matches_open_file(current, opened_after)
            or _active_note_file_identity(opened)
            != _active_note_file_identity(opened_after)
            or not stat.S_ISREG(opened_after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(opened_after)
            or _is_reparse_point(current)
        ):
            raise OSError("active note changed while reading")
    try:
        content = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise OSError("active note is not strict UTF-8") from exc
    return BoundedNoteSnapshot(
        content=content,
        source_bytes=raw,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        file_identity=_active_note_file_identity(opened_after),
        byte_size=len(raw),
    )


def read_bounded_note(path: Path, max_bytes: int = MAX_ACTIVE_NOTE_BYTES) -> str:
    """Read one regular UTF-8 note through a stable, no-follow parent binding."""
    return read_bounded_note_snapshot(path, max_bytes).content


def parse_active_note_metadata(content: str) -> ActiveNoteMetadata | None:
    """Return normalized active metadata, or None for inactive/invalid pages."""
    status = parse_frontmatter_scalar(content, "status")
    project = parse_project_scope(content)
    page_type = parse_frontmatter_scalar(content, "type")
    authority = parse_frontmatter_scalar(content, "source_authority")
    confidence = parse_frontmatter_scalar(content, "confidence")
    if any(
        field.present and field.value is None
        for field in (status, project, page_type, authority, confidence)
    ):
        return None
    if status.value is not None and status.value.casefold() in {
        "archived",
        "superseded",
    }:
        return None
    authority_value = (authority.value or "inferred").casefold()
    confidence_value = (confidence.value or "low").casefold()
    if authority_value not in AUTHORITY_RANKS or confidence_value not in CONFIDENCE_RANKS:
        return None
    return ActiveNoteMetadata(
        page_type=(page_type.value or "").casefold(),
        project=project.value or "",
        authority=authority_value,
        confidence=confidence_value,
    )


def _logical_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    visible = "".join(
        char if unicodedata.category(char)[:1] in {"L", "N"} else " "
        for char in normalized
    )
    return " ".join(visible.split())


def active_note_logical_identities(slug: str, title: str) -> frozenset[str]:
    """Return every non-empty identity used by canonical note selection."""
    return frozenset(
        identity
        for identity in (_logical_identity(slug), _logical_identity(title))
        if identity
    )


def active_note_generation_manifest(
    selection: ActiveNoteSelection,
    artifact: str,
) -> dict[str, object]:
    """Return versioned metadata binding a derived artifact to one selection."""
    return {
        "version": selection.generation.version,
        "artifact": artifact,
        "canonical_sha256": selection.generation.canonical_sha256,
        "paths": [note.relative_path for note in selection.notes],
    }


def is_safe_root_relative_markdown_path(value: str) -> bool:
    """Return whether value is exact safe ROOT-relative POSIX Markdown syntax."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
    ):
        return False
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.anchor
        or parsed.as_posix() != value
        or parsed.suffix != ".md"
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        return False
    return all(
        not (
            ord(char) < 32
            or 127 <= ord(char) <= 159
            or char in UNSAFE_MARKDOWN_PATH_DELIMITERS
            or ord(char) in {0x2028, 0x2029}
            or 0xD800 <= ord(char) <= 0xDFFF
            or _is_unicode_noncharacter(ord(char))
        )
        for part in parsed.parts
        for char in part
    )


def _diagnostic_text(value: str) -> str:
    if len(value) <= MAX_DIAGNOSTIC_TEXT_CHARS:
        return value
    return value[: MAX_DIAGNOSTIC_TEXT_CHARS - 3] + "..."


def _first_visible_h1(content: str) -> str | None:
    from session_start_project_state import _state_visible_lines

    original_lines = content.splitlines()
    visible_lines = _state_visible_lines(content)
    for original, visible in zip(original_lines, visible_lines, strict=True):
        if _H1_OPEN_RE.match(original) is None:
            continue
        match = _H1_RE.fullmatch(visible)
        if match is None:
            continue
        title = (match.group(1) or "").strip(" \t")
        return re.sub(r"[ \t]+#+[ \t]*$", "", title).rstrip(" \t")
    return None


def active_note_page_logical_identities(slug: str, content: str) -> frozenset[str]:
    """Parse the identities canonical selection assigns to rendered page content."""
    visible_title = _first_visible_h1(content)
    title = slug if visible_title is None else visible_title
    return active_note_logical_identities(slug, title)


def select_active_notes(
    notes_root: Path,
    *,
    root: Path | None = None,
    max_entries: int = MAX_ACTIVE_NOTE_ENTRIES,
    max_page_bytes: int = MAX_ACTIVE_NOTE_BYTES,
    max_total_bytes: int = MAX_ACTIVE_NOTE_TOTAL_BYTES,
    max_diagnostics: int = MAX_DUPLICATE_DIAGNOSTICS,
    max_diagnostic_shadows: int = MAX_DIAGNOSTIC_SHADOWS,
) -> ActiveNoteSelection:
    """Select one deterministic active canonical page per logical identity."""
    if min(
        max_entries,
        max_page_bytes,
        max_total_bytes,
        max_diagnostics,
        max_diagnostic_shadows,
    ) < 0:
        raise OSError("active note selection limit is invalid")
    note_root = Path(os.path.abspath(notes_root))
    vault_root = Path(os.path.abspath(root)) if root is not None else note_root
    inventory = bounded_path_inventory(
        note_root,
        "*.md",
        max_entries,
        recursive=True,
        kind="file",
    )
    if inventory.incomplete:
        raise OSError("active note inventory is incomplete or unsafe")

    candidates: list[ActiveNote] = []
    inventory_records: list[dict[str, object]] = []
    unsafe_relative_paths: list[str] = []
    total_bytes = 0
    for path in inventory.paths:
        try:
            relative_to_notes = path.relative_to(note_root)
            relative_to_root = path.relative_to(vault_root).as_posix()
        except ValueError as exc:
            raise OSError("active note inventory escaped its root") from exc
        if not is_safe_root_relative_markdown_path(relative_to_root):
            unsafe_relative_paths.append(relative_to_root)
            inventory_records.append(
                {"kind": "unsafe-path", "path": relative_to_root}
            )
            continue
        if is_editorial_name(path.name):
            inventory_records.append(
                {"kind": "editorial", "path": relative_to_root}
            )
            continue
        if any(part.casefold() == "archive" for part in relative_to_notes.parts[:-1]):
            inventory_records.append(
                {"kind": "archive", "path": relative_to_root}
            )
            continue
        snapshot = read_bounded_note_snapshot(path, max_page_bytes)
        inventory_records.append(
            {
                "file_identity": _file_identity_record(snapshot.file_identity),
                "path": relative_to_root,
                "sha256": snapshot.content_sha256,
            }
        )
        content = snapshot.content
        total_bytes += snapshot.byte_size
        if total_bytes > max_total_bytes:
            raise OSError("active note aggregate byte limit exceeded")
        metadata = parse_active_note_metadata(content)
        if metadata is None:
            continue
        visible_title = _first_visible_h1(content)
        title = path.stem if visible_title is None else visible_title
        candidates.append(
            ActiveNote(
                path=path,
                relative_path=relative_to_root,
                content=content,
                source_bytes=snapshot.source_bytes,
                content_sha256=snapshot.content_sha256,
                file_identity=snapshot.file_identity,
                title=title,
                slug=path.stem,
                page_type=metadata.page_type,
                project=metadata.project,
                authority=metadata.authority,
                confidence=metadata.confidence,
                authority_rank=AUTHORITY_RANKS[metadata.authority],
                confidence_rank=CONFIDENCE_RANKS[metadata.confidence],
                is_flat=len(relative_to_notes.parts) == 1,
            )
        )

    parents = list(range(len(candidates)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parents[right_root] = left_root
        else:
            parents[left_root] = right_root

    identity_owner: dict[str, int] = {}
    identities_by_index: list[tuple[str, ...]] = []
    invalid_identity_indices: list[int] = []
    for index, note in enumerate(candidates):
        identities = tuple(
            sorted(active_note_page_logical_identities(note.slug, note.content))
        )
        identities_by_index.append(identities)
        if not identities:
            invalid_identity_indices.append(index)
            continue
        for identity in identities:
            owner = identity_owner.setdefault(identity, index)
            union(index, owner)

    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        if not identities_by_index[index]:
            continue
        groups.setdefault(find(index), []).append(index)

    def preference(index: int) -> tuple[int, int, int, str, str]:
        note = candidates[index]
        return (
            -int(note.is_flat),
            -note.authority_rank,
            -note.confidence_rank,
            note.relative_path.casefold(),
            note.relative_path,
        )

    winners: list[ActiveNote] = []
    all_diagnostics: list[DuplicateDiagnostic] = []
    if unsafe_relative_paths:
        rejected_paths = sorted(
            unsafe_relative_paths,
            key=lambda value: (value.casefold(), value),
        )
        shown = tuple(
            _diagnostic_text(json.dumps(value, ensure_ascii=True)[1:-1])
            for value in rejected_paths[:max_diagnostic_shadows]
        )
        all_diagnostics.append(
            DuplicateDiagnostic(
                identity="<unsafe relative path>",
                canonical="",
                shadows=shown,
                shadow_count=len(rejected_paths),
                shadows_truncated=len(rejected_paths) > len(shown),
                kind="unsafe-path",
            )
        )
    if invalid_identity_indices:
        rejected_paths = sorted(
            (candidates[index].relative_path for index in invalid_identity_indices),
            key=lambda value: (value.casefold(), value),
        )
        shown = tuple(
            _diagnostic_text(value)
            for value in rejected_paths[:max_diagnostic_shadows]
        )
        all_diagnostics.append(
            DuplicateDiagnostic(
                identity="<empty logical identity>",
                canonical="",
                shadows=shown,
                shadow_count=len(rejected_paths),
                shadows_truncated=len(rejected_paths) > len(shown),
                kind="invalid-identity",
            )
        )
    for indices in groups.values():
        ordered = sorted(indices, key=preference)
        winner = candidates[ordered[0]]
        winners.append(winner)
        if len(ordered) == 1:
            continue
        shadow_paths = sorted(
            (candidates[index].relative_path for index in ordered[1:]),
            key=lambda value: (value.casefold(), value),
        )
        group_identities = sorted(
            {identity for index in ordered for identity in identities_by_index[index]}
        )
        identity = group_identities[0] if group_identities else winner.relative_path
        shown = tuple(
            _diagnostic_text(value)
            for value in shadow_paths[:max_diagnostic_shadows]
        )
        all_diagnostics.append(
            DuplicateDiagnostic(
                identity=_diagnostic_text(identity),
                canonical=_diagnostic_text(winner.relative_path),
                shadows=shown,
                shadow_count=len(shadow_paths),
                shadows_truncated=len(shadow_paths) > len(shown),
            )
        )

    winners.sort(key=lambda note: (note.relative_path.casefold(), note.relative_path))
    all_diagnostics.sort(
        key=lambda item: (item.identity.casefold(), item.identity, item.canonical)
    )
    shown_diagnostics = tuple(all_diagnostics[:max_diagnostics])
    canonical_records: list[dict[str, object]] = [
        {
            "path": note.relative_path,
            "sha256": note.content_sha256,
        }
        for note in winners
    ]
    return ActiveNoteSelection(
        notes=tuple(winners),
        diagnostics=shown_diagnostics,
        diagnostics_truncated=len(all_diagnostics) > len(shown_diagnostics),
        candidate_identities=frozenset(identity_owner),
        generation=ActiveNoteGeneration(
            version=ACTIVE_NOTE_GENERATION_VERSION,
            inventory_sha256=_generation_sha256(inventory_records),
            canonical_sha256=_generation_sha256(canonical_records),
        ),
    )

# Pages intentionally exempt from orphan / sparse / backlink checks.
# Indexes, logs, and human front doors are editorial metadata; project
# state pages are auto-updated "where we left off" records with the
# same rationale.
EDITORIAL_NAMES: frozenset[str] = frozenset({
    "index.md",
    "log.md",
    "Vault Home.md",
    "AGENTS.md",
    "operating-model.md",
    "guardrails.md",
    # Directory-level readmes are metadata, not curated content.
    "README.md",
    # Per-project state pages under `knowledge/projects/<slug>/state.md` are
    # auto-updated by the SessionStart hook. Same rationale as index/log.
    "state.md",
    "context.md",
})

# Pages that point DOWN to concepts but shouldn't impose BACKLINK
# obligations on everything that links UP to them.
BACKLINK_EXEMPT_NAMES: frozenset[str] = frozenset({
    "Ingestion Workflow.md",
    "Retrieval Workflow.md",
    "Review Workflow.md",
    "Vault Home.md",
    "index.md",
    "log.md",
    # Utility synthesis cited by many concepts — would otherwise require
    # a backlink on every concept page.
    "Karpathy LLM Wiki Workflow.md",
})

# Files whose prose frequently contains bracketed literals that look like
# wikilinks but are code fences / placeholders / historical references.
BROKEN_LINK_SKIP_NAMES: frozenset[str] = frozenset({
    "operating-model.md",
    "AGENTS.md",
    # log.md is editorial changelog — historical entries may cite pages
    # that were later renamed or contain literal bracketed strings as
    # examples. Append-only, so rewriting is not an option.
    "log.md",
})


def editorial_parents_to_skip(wiki_root: Path) -> tuple[Path, ...]:
    """Directory paths to treat as editorial scaffolding (skip entirely).

    Returns resolved paths so callers can do a `parent in skip` check
    without worrying about relative/absolute or case mismatches.
    """
    return (
        (wiki_root / "projects" / "_template").resolve(),
    )


def is_editorial_name(filename: str) -> bool:
    """True if *filename* is in EDITORIAL_NAMES."""
    return filename in EDITORIAL_NAMES


def is_backlink_exempt(filename: str) -> bool:
    """True if the page is exempt from the backlink-reciprocity check.

    Editorial names are always backlink-exempt; so are the explicit
    BACKLINK_EXEMPT_NAMES (workflows and utility syntheses).
    """
    return filename in EDITORIAL_NAMES or filename in BACKLINK_EXEMPT_NAMES
