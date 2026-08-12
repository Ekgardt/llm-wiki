"""Audit and transactionally remove narrowly verified installed-memory noise.

Backup-only prepares an unapproved sealed manifest and private temporary source
staging. Approved apply revalidates every source under the repair and writer
locks, rolls back ordinary failures, and purges schema-v4 staging after commit.
Completed v4 removal has no source backup or rollback; interrupted schema-v3
transactions retain their legacy recovery artifacts. Ambiguous content and
service-session inventories are report-only or out of scope.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 4
LEGACY_SCHEMA_VERSION = 3
TOOL_LINE_RE = re.compile(
    r"(?m)^[ \t]*- `\[(?P<time>\d{2}:\d{2}:\d{2})\] tool \| [^`\r\n]*`[ \t]*(?:\r?\n|$)"
)
IDLE_BLOCK_RE = re.compile(
    r"(?mis)^## \[(?P<time>\d{2}:\d{2}:\d{2})\] (?P<header>[^\r\n]*idle[^\r\n]*)\r?\n"
    r"(?P<body>.*?)(?=^## \[|\Z)"
)
EMPTY_BODY_MARKERS = {"(no body)", "(empty)", "- (no body)", "- (empty)"}
METADATA_PREFIXES = ("- tier:", "- trigger:", "- slug:", "- project root:")
DAILY_HEADER_RE = re.compile(
    r"^ {0,3}#(?: Daily(?: Log)?(?: [\-\N{EM DASH}]? ?\d{4}-\d{2}-\d{2})?|"
    r" Daily Session Memory [\-\N{EM DASH}] \d{4}-\d{2}-\d{2}|"
    r" \d{4}-\d{2}-\d{2})[ \t]*$",
    re.IGNORECASE,
)
DAILY_CAPTURE_PROVENANCE_RE = re.compile(
    r"^\s*<!--\s*llm-wiki-capture\s*:",
    re.IGNORECASE,
)
BACKUP_STAMP_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z(?:-\d+)?$")
MUTATING_ACTIONS = frozenset(
    {
        "delete_exact_duplicate_note",
        "delete_stale_note",
        "delete_false_feedback",
        "delete_generated_daily",
        "mark_handoff_unavailable",
    }
)
LEGACY_MUTATING_ACTIONS = frozenset({"clean_daily", "quarantine"})
LEGACY_REPORT_ACTIONS = frozenset(
    {"preserve", "review", "propose_safe_api_delete"}
)
LEGACY_ACTIONS = LEGACY_MUTATING_ACTIONS | LEGACY_REPORT_ACTIONS
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_REPAIR_INVENTORY_ENTRIES = 10_000
MAX_REPAIR_DAILY_BYTES = 4 * 1024 * 1024
MAX_REPAIR_DAILY_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REPAIR_FEEDBACK_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REPAIR_PROJECT_TOTAL_BYTES = 64 * 1024 * 1024


class RepairError(RuntimeError):
    """A safety contract prevented the requested operation."""


class TransactionError(RepairError):
    """A commit failed and transaction recovery was attempted."""


class PreMutationError(TransactionError):
    """Validation failed before the current source could be mutated."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _candidate(
    candidate_id: str,
    kind: str,
    path_id: str,
    action: str,
    before: str,
    after: str,
    reason: str,
    status: str = "candidate",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": candidate_id,
        "kind": kind,
        "path_id": path_id,
        "action": action,
        "before_sha256": before,
        "after_sha256": after,
        "reason": reason,
        "status": status,
        "metadata": metadata or {},
    }


def _summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(candidates),
        "by_action": dict(sorted(Counter(c["action"] for c in candidates).items())),
        "by_kind": dict(sorted(Counter(c["kind"] for c in candidates).items())),
        "by_status": dict(sorted(Counter(c["status"] for c in candidates).items())),
    }


def _report(
    mode: str,
    status: str,
    vault: Path,
    candidates: list[dict[str, Any]],
    manifest: Path | None = None,
    stale_pages: tuple[str, ...] = (),
    diagnostics: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    ordered = sorted(candidates, key=lambda c: (c["path_id"], c["kind"], c["id"]))
    ordered_diagnostics = sorted(
        diagnostics,
        key=lambda item: (item["path_id"], item["kind"], item["id"]),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "root_fingerprint": _root_fingerprint(vault),
        "stale_pages": list(stale_pages),
        "backup_manifest": str(manifest) if manifest else None,
        "candidates": ordered,
        "diagnostics": ordered_diagnostics,
        "summary": _summary(ordered),
    }


def _root_fingerprint(vault: Path) -> str:
    return _sha(os.path.normcase(str(vault.resolve())).encode())


def _opaque_path_id(vault: Path, path: Path, namespace: str) -> str:
    rel = path.relative_to(vault).as_posix()
    material = f"{_root_fingerprint(vault)}\0{namespace}\0{rel}".encode()
    return _sha(material)


def _path_is_link_or_reparse(path: Path) -> bool:
    """Reject POSIX symlinks and every Windows reparse-point type."""
    if path.is_symlink():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_link_components(path: Path, root: Path) -> None:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RepairError(f"path escapes allowed root: {path}") from exc
    current = root
    for component in (Path(), *relative.parts):
        if component != Path():
            current = current / component
        if _path_is_link_or_reparse(current):
            raise RepairError(f"symlink or reparse path is not allowed: {current}")


def _resolved_containment(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=True)
    current = path
    while not current.exists() and current != root:
        current = current.parent
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise RepairError(f"cannot resolve path: {current}: {exc}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise RepairError(f"resolved path escapes allowed root: {path}")


def _regular_file(path: Path, root: Path) -> Path:
    _reject_link_components(path, root)
    _resolved_containment(path, root)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RepairError(f"source is not a regular file: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _path_is_link_or_reparse(path)
        or metadata.st_nlink != 1
    ):
        raise RepairError(f"source is not a regular file: {path}")
    return path


def _safe_files(root: Path, suffix: str) -> list[Path]:
    if not root.exists():
        return []
    _reject_link_components(root, root)
    files: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *names]:
            child = current_path / name
            if _path_is_link_or_reparse(child):
                raise RepairError(f"symlink or reparse path is not allowed: {child}")
        for name in names:
            path = current_path / name
            if path.suffix.casefold() == suffix.casefold():
                files.append(_regular_file(path, root))
    return sorted(files)


def _repair_zone_files(
    root: Path,
    pattern: str,
    *,
    recursive: bool,
) -> list[Path]:
    from memory_state import bounded_path_inventory

    inventory_result = bounded_path_inventory(
        root,
        pattern,
        MAX_REPAIR_INVENTORY_ENTRIES,
        recursive=recursive,
        kind="file",
    )
    if inventory_result.incomplete:
        raise RepairError(f"repair inventory is incomplete or unsafe: {root.name}")
    return list(inventory_result.paths)


def _safe_mkdir(path: Path, root: Path) -> None:
    _reject_link_components(path, root)
    _resolved_containment(path, root)
    relative = path.absolute().relative_to(root.absolute())
    current = root
    for component in relative.parts:
        current = current / component
        if _path_is_link_or_reparse(current):
            raise RepairError(f"symlink or reparse path is not allowed: {current}")
        current.mkdir(exist_ok=True)
        try:
            current.chmod(0o700)
        except OSError:
            if os.name != "nt":
                raise
        if _path_is_link_or_reparse(current):
            raise RepairError(f"created path became a reparse point: {current}")


def _is_empty_idle_body(body: str) -> bool:
    for line in body.splitlines():
        stripped = line.strip()
        lowered = stripped.casefold()
        if not stripped or lowered in EMPTY_BODY_MARKERS:
            continue
        if lowered.startswith(METADATA_PREFIXES):
            continue
        return False
    return True


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _daily_analysis(data: bytes, path_id: str) -> tuple[dict[str, Any] | None, bytes]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, data
    lines = text.splitlines()
    if any(DAILY_CAPTURE_PROVENANCE_RE.match(line) for line in lines):
        return None, data

    covered = [False] * len(lines)
    header_count = 0
    empty_tool_count = 0
    generated_record_count = 0
    for index, line in enumerate(lines):
        if DAILY_HEADER_RE.fullmatch(line):
            covered[index] = True
            header_count += 1
        elif TOOL_LINE_RE.fullmatch(line):
            covered[index] = True
            empty_tool_count += 1

    try:
        from session_start_context import parse_daily_records

        records = parse_daily_records(text)
    except (MemoryError, RuntimeError, ValueError):
        return None, data
    for record in records:
        event = record.event.casefold()
        generated_event = event in {
            "opencode-idle",
            "session-end",
            "pre-compact",
            "service",
            "status",
            "shell",
        }
        if record.kind != "heading" or record.meaningful or not generated_event:
            return None, data
        source_lines = record.source_lines or record.lines
        end = record.source_position + len(source_lines)
        if end > len(lines):
            return None, data
        for index in range(record.source_position, end):
            covered[index] = True
        generated_record_count += 1

    if any(line.strip() and not covered[index] for index, line in enumerate(lines)):
        return None, data
    metadata = {
        "header_count": header_count,
        "empty_tool_breadcrumb_count": empty_tool_count,
        "generated_record_count": generated_record_count,
        "nonblank_line_count": sum(bool(line.strip()) for line in lines),
    }
    candidate = _candidate(
        f"generated_daily:{path_id}",
        "generated_daily",
        path_id,
        "delete_generated_daily",
        _sha(data),
        _sha(b""),
        "complete line coverage proves a generated or empty whole daily file",
        metadata=metadata,
    )
    return candidate, b""


def _daily_candidates(path: Path, vault: Path) -> tuple[list[dict[str, Any]], bytes]:
    from vault_editorial import read_bounded_note_snapshot

    path_id = _opaque_path_id(vault, path, "daily")
    try:
        data = read_bounded_note_snapshot(path, MAX_REPAIR_DAILY_BYTES).source_bytes
    except OSError as exc:
        raise RepairError(f"daily source is unsafe or oversized: {path.name}") from exc
    candidate, cleaned = _daily_analysis(data, path_id)
    if candidate is not None:
        return [candidate], cleaned
    digest = _sha(data)
    return [
        _candidate(
            f"daily_preserved:{path_id}",
            "daily_preserved",
            path_id,
            "preserve",
            digest,
            digest,
            "daily retained because complete generated-only coverage was not proven",
            "preserved",
            {"classification": "mixed_malformed_unrecognized_or_provenanced"},
        )
    ], data


def _feedback_candidates(path: Path, vault: Path) -> list[dict[str, Any]]:
    from feedback_capture import MAX_FEEDBACK_BYTES, _read_feedback_candidate
    from vault_editorial import read_bounded_note_snapshot

    try:
        record = _read_feedback_candidate(path)
        snapshot = read_bounded_note_snapshot(path, MAX_FEEDBACK_BYTES)
    except (OSError, RuntimeError) as exc:
        raise RepairError(f"feedback source is unsafe or oversized: {path.name}") from exc
    path_id = _opaque_path_id(vault, path, "feedback")
    if record is None:
        return [
            _candidate(
                f"feedback_preserved:{path_id}",
                "feedback_preserved",
                path_id,
                "preserve",
                snapshot.content_sha256,
                snapshot.content_sha256,
                "feedback retained because canonical parsing rejected it",
                "preserved",
                {"classification": "malformed"},
            )
        ]
    try:
        from memory_state import decode_json_object_strict

        stable_record = decode_json_object_strict(
            snapshot.source_bytes,
            max_bytes=MAX_FEEDBACK_BYTES,
        )
    except (UnicodeError, ValueError, TypeError, RecursionError, MemoryError):
        return []
    if stable_record != record:
        raise RepairError(f"feedback changed during canonical parsing: {path.name}")
    digest = snapshot.content_sha256
    is_direct_user = record.get("source_role") == "user" or record.get(
        "trigger"
    ) == "opencode-user-message"
    is_generated_idle = (
        record.get("trigger") == "opencode-idle"
        and record.get("status") == "candidate"
        and not is_direct_user
    )
    if is_generated_idle:
        return [
            _candidate(
                f"false_feedback:{path_id}",
                "false_feedback",
                path_id,
                "delete_false_feedback",
                digest,
                _sha(b""),
                "canonically parsed generated-idle feedback candidate",
                metadata={"classification": "generated_idle_candidate"},
            )
        ]
    if record.get("status") == "promoted":
        classification = "promoted"
    elif record.get("status") == "rejected":
        classification = "rejected"
    elif is_direct_user:
        classification = "direct_user"
    else:
        classification = "ambiguous_provenance"
    return [
        _candidate(
            f"feedback_preserved:{path_id}",
            "feedback_preserved",
            path_id,
            "preserve",
            digest,
            digest,
            "feedback retained because it is true or ambiguous",
            "preserved",
            {"classification": classification},
        )
    ]


def _title_summary_keys(data: bytes, fallback: str) -> tuple[str, str]:
    text = data.decode("utf-8", errors="replace")
    title = fallback
    summary = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith("one-sentence summary:"):
            summary = stripped.split(":", 1)[1].strip()
            break
    def normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    return normalize(title), normalize(summary)


def _duplicate_candidate(
    kind: str,
    group_id: str,
    category: str,
    score: float,
    member_ids: list[str],
    member_hashes: list[str],
) -> dict[str, Any]:
    group_hash = _sha(_json_bytes(member_hashes))
    return _candidate(
        f"{kind}:{group_id}",
        kind,
        group_id,
        "review",
        group_hash,
        group_hash,
        "duplicate-note review group",
        "review_only",
        {
            "category": category,
            "similarity_score": score,
            "member_count": len(member_ids),
            "member_ids": member_ids,
            "member_ids_sha256": _sha(_json_bytes(member_ids)),
        },
    )


def _duplicate_candidates(vault: Path) -> list[dict[str, Any]]:
    from vault_editorial import read_bounded_note_snapshot, select_active_notes

    try:
        selection = select_active_notes(
            vault / "knowledge" / "notes",
            root=vault,
            max_entries=MAX_REPAIR_INVENTORY_ENTRIES,
            max_diagnostics=MAX_REPAIR_INVENTORY_ENTRIES,
            max_diagnostic_shadows=MAX_REPAIR_INVENTORY_ENTRIES,
        )
    except OSError as exc:
        raise RepairError(f"active note selection failed: {exc}") from exc
    if selection.diagnostics_truncated or any(
        diagnostic.shadows_truncated for diagnostic in selection.diagnostics
    ):
        raise RepairError("active note duplicate diagnostics are incomplete")

    canonical = {note.relative_path: note for note in selection.notes}
    candidates: list[dict[str, Any]] = []
    for diagnostic in selection.diagnostics:
        winner = canonical.get(diagnostic.canonical)
        if diagnostic.kind != "duplicate":
            continue
        if winner is None:
            raise RepairError("active note duplicate diagnostic canonical path is unavailable")
        nonidentical_ids: list[str] = []
        nonidentical_hashes: list[str] = []
        for relative in diagnostic.shadows:
            shadow = vault / relative
            try:
                snapshot = read_bounded_note_snapshot(shadow)
            except OSError as exc:
                raise RepairError(f"duplicate shadow snapshot failed: {exc}") from exc
            path_id = _opaque_path_id(vault, shadow, "note")
            if snapshot.source_bytes == winner.source_bytes:
                candidates.append(
                    _candidate(
                        f"exact_duplicate_shadow:{path_id}",
                        "exact_duplicate_shadow",
                        path_id,
                        "delete_exact_duplicate_note",
                        snapshot.content_sha256,
                        _sha(b""),
                        "byte-exact non-canonical shadow selected by active-note diagnostics",
                        metadata={
                            "canonical_path_id": _opaque_path_id(
                                vault, winner.path, "note"
                            ),
                            "classification": "byte_exact_shadow",
                        },
                    )
                )
            else:
                nonidentical_ids.append(path_id)
                nonidentical_hashes.append(snapshot.content_sha256)
        if nonidentical_ids:
            winner_id = _opaque_path_id(vault, winner.path, "note")
            member_ids = sorted([winner_id, *nonidentical_ids])
            group_id = f"group-{_sha(_json_bytes(member_ids))[:24]}"
            candidates.append(
                _duplicate_candidate(
                    "semantic_duplicate_note",
                    group_id,
                    "selector_nonidentical_shadow",
                    0.0,
                    member_ids,
                    sorted([winner.content_sha256, *nonidentical_hashes]),
                )
            )
    return candidates


def _stale_candidates(vault: Path, stale_pages: tuple[str, ...]) -> list[dict[str, Any]]:
    from vault_editorial import is_safe_root_relative_markdown_path, select_active_notes

    if not stale_pages:
        return []
    try:
        selection = select_active_notes(
            vault / "knowledge" / "notes",
            root=vault,
            max_entries=MAX_REPAIR_INVENTORY_ENTRIES,
        )
    except OSError as exc:
        raise RepairError(f"active note selection failed: {exc}") from exc
    canonical = {note.relative_path: note for note in selection.notes}
    candidates = []
    seen: dict[str, str] = {}
    for relative in stale_pages:
        key = relative.casefold()
        if key in seen:
            raise RepairError(f"duplicate or case-alias stale page path: {relative!r}")
        seen[key] = relative
        parsed = PurePosixPath(relative)
        if (
            not is_safe_root_relative_markdown_path(relative)
            or parsed.parts[:2] != ("knowledge", "notes")
        ):
            raise RepairError(f"unsafe stale page path: {relative!r}")
        note = canonical.get(relative)
        if note is None:
            raise RepairError(f"stale page is not an exact active canonical note: {relative!r}")
        path_id = _opaque_path_id(vault, note.path, "note")
        candidates.append(
            _candidate(
                f"stale_note:{path_id}",
                "stale_note",
                path_id,
                "delete_stale_note",
                note.content_sha256,
                _sha(b""),
                "explicit operator-provided active stale note",
                metadata={"classification": "explicit_stale_active_canonical"},
            )
        )
    return candidates


def _project_state_replacement(data: bytes) -> bytes | None:
    from session_start_project_state import (
        STATE_SECTION_TEMPLATE_PLACEHOLDERS,
        _state_h2_title,
        _state_visible_lines,
    )

    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    placeholder = STATE_SECTION_TEMPLATE_PLACEHOLDERS["where we left off"]
    original_lines = body.splitlines(keepends=True)
    sentinel = "LLM_WIKI_EXACT_HANDOFF_PLACEHOLDER_4D6E7B8A"
    masked_lines = []
    for line in original_lines:
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        masked_lines.append(
            content.replace(placeholder, sentinel) + ending
            if content.strip() == placeholder
            else line
        )
    visible_lines = _state_visible_lines("".join(masked_lines))
    current_section = ""
    matches: list[int] = []
    for index, visible in enumerate(visible_lines):
        title = _state_h2_title(visible)
        if title is not None:
            current_section = title
            continue
        if current_section == "where we left off" and visible.strip() == sentinel:
            matches.append(index)
    if len(matches) != 1:
        return None
    index = matches[0]
    original = original_lines[index]
    content = original.rstrip("\r\n")
    ending = original[len(content) :]
    offset = content.find(placeholder)
    if offset < 0 or content[:offset].strip() or content[offset + len(placeholder) :].strip():
        return None
    original_lines[index] = (
        content[:offset]
        + "(saved project handoff unavailable)"
        + content[offset + len(placeholder) :]
        + ending
    )
    return "".join(original_lines).encode("utf-8")


def _project_state_candidates(vault: Path) -> list[dict[str, Any]]:
    from memory_state import bounded_path_inventory
    from session_start_project_state import (
        MAX_PROJECT_STATE_OWNERSHIP_CHARS,
        _recorded_project_root,
        _trusted_state_body_matches_identity,
    )
    from vault_editorial import read_bounded_note_snapshot

    projects = vault / "knowledge" / "projects"
    inventory = bounded_path_inventory(
        projects,
        "state.md",
        MAX_REPAIR_INVENTORY_ENTRIES,
        recursive=True,
        kind="file",
    )
    if inventory.incomplete:
        raise RepairError("project-state inventory is incomplete or unsafe")
    candidates = []
    total_bytes = 0
    for path in inventory.paths:
        relative = path.relative_to(projects)
        if len(relative.parts) != 2 or relative.parts[1] != "state.md":
            continue
        slug = relative.parts[0]
        if slug.casefold() == "_template":
            continue
        try:
            snapshot = read_bounded_note_snapshot(
                path,
                MAX_PROJECT_STATE_OWNERSHIP_CHARS,
            )
        except OSError:
            continue
        total_bytes += snapshot.byte_size
        if total_bytes > MAX_REPAIR_PROJECT_TOTAL_BYTES:
            raise RepairError("project-state aggregate byte limit exceeded")
        recorded_root = _recorded_project_root(snapshot.content)
        if recorded_root is None or not _trusted_state_body_matches_identity(
            snapshot.content,
            slug,
            Path(recorded_root),
        ):
            continue
        replacement = _project_state_replacement(snapshot.source_bytes)
        if replacement is None:
            continue
        path_id = _opaque_path_id(vault, path, "project-state")
        candidates.append(
            _candidate(
                f"project_handoff_placeholder:{path_id}",
                "project_handoff_placeholder",
                path_id,
                "mark_handoff_unavailable",
                snapshot.content_sha256,
                _sha(replacement),
                "exact visible handoff placeholder in canonically owned project state",
                metadata={"classification": "trusted_visible_placeholder"},
            )
        )
    return candidates


def _session_candidates(sessions_file: Path | None) -> list[dict[str, Any]]:
    if sessions_file is None:
        return []
    if _path_is_link_or_reparse(sessions_file) or not sessions_file.is_file():
        raise RepairError(f"sessions file must be a non-link regular file: {sessions_file}")
    try:
        records = json.loads(sessions_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairError(f"cannot read sessions file: {sessions_file}: {exc}") from exc
    if not isinstance(records, list):
        raise RepairError("sessions file must contain a JSON array")
    candidates = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = record.get("title")
        orphaned = record.get("orphaned") is True or record.get("status") == "orphaned"
        if not isinstance(title, str) or not title.startswith("memory-") or not orphaned:
            continue
        digest = _sha(json.dumps(record, sort_keys=True).encode("utf-8"))
        session_id = _sha(f"session\0{digest}".encode())
        candidates.append(
            _candidate(
                f"orphan_service_session:{session_id}",
                "orphan_service_session",
                session_id,
                "propose_safe_api_delete",
                digest,
                digest,
                "strict service-title and orphan evidence; no safe API contract supplied",
                "unsupported_safe_api",
                {"title_prefix": "memory-", "orphan_evidence": True},
            )
        )
    return candidates


def inventory(
    vault: Path,
    state_root: Path,
    sessions_file: Path | None = None,
    stale_pages: tuple[str, ...] = (),
) -> dict[str, Any]:
    vault = vault.resolve(strict=True)
    state_root = state_root.absolute()
    if sessions_file is not None:
        raise RepairError("session and terminal-task inventories are out of scope")
    candidates: list[dict[str, Any]] = []
    daily_paths = _repair_zone_files(
        vault / "knowledge" / "daily",
        "*.md",
        recursive=False,
    )
    if sum(path.lstat().st_size for path in daily_paths) > MAX_REPAIR_DAILY_TOTAL_BYTES:
        raise RepairError("daily source aggregate byte limit exceeded")
    for path in daily_paths:
        found, _cleaned = _daily_candidates(path, vault)
        candidates.extend(found)
    feedback_paths = _repair_zone_files(
        vault / "knowledge" / "feedback",
        "*.json",
        recursive=False,
    )
    if sum(path.lstat().st_size for path in feedback_paths) > MAX_REPAIR_FEEDBACK_TOTAL_BYTES:
        raise RepairError("feedback source aggregate byte limit exceeded")
    for path in feedback_paths:
        candidates.extend(_feedback_candidates(path, vault))
    candidates.extend(_duplicate_candidates(vault))
    candidates.extend(_stale_candidates(vault, stale_pages))
    candidates.extend(_project_state_candidates(vault))
    actionable = [
        candidate for candidate in candidates if candidate.get("action") in MUTATING_ACTIONS
    ]
    diagnostics = [
        candidate for candidate in candidates if candidate.get("action") not in MUTATING_ACTIONS
    ]
    return _report(
        "audit",
        "ok",
        vault,
        actionable,
        stale_pages=stale_pages,
        diagnostics=diagnostics,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_new_file(path: Path, data: bytes, root: Path) -> None:
    _reject_link_components(path, root)
    _resolved_containment(path, root)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_write(path: Path, data: bytes, root: Path) -> None:
    _reject_link_components(path, root)
    _resolved_containment(path, root)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _durable_new_file(temporary, data, root)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _expected_hashes(
    candidates: list[dict[str, Any]],
    actions: frozenset[str] = MUTATING_ACTIONS,
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for candidate in candidates:
        if candidate["action"] not in actions:
            continue
        path_id = candidate.get("path_id")
        if not isinstance(path_id, str) or len(path_id) < 32:
            raise RepairError("mutation candidate identity is invalid")
        previous = expected.setdefault(path_id, candidate["before_sha256"])
        if previous != candidate["before_sha256"]:
            raise RepairError(f"inconsistent candidate hashes for {path_id}")
    return expected


def _mutation_contracts(
    candidates: list[dict[str, Any]],
    actions: frozenset[str] = MUTATING_ACTIONS,
) -> dict[str, tuple[str, str]]:
    contracts: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        action = candidate.get("action")
        if action not in actions:
            continue
        path_id = candidate.get("path_id")
        if not isinstance(path_id, str) or len(path_id) < 32:
            raise RepairError("mutation candidate identity is invalid")
        contract = (action, candidate.get("after_sha256"))
        previous = contracts.setdefault(path_id, contract)
        if previous != contract:
            raise RepairError(f"inconsistent mutation contract for {path_id}")
    return contracts


def _validate_audit_report(report: dict[str, Any], vault: Path, state_root: Path) -> None:
    expected_keys = {
        "schema_version",
        "mode",
        "status",
        "root_fingerprint",
        "stale_pages",
        "backup_manifest",
        "candidates",
        "diagnostics",
        "summary",
    }
    if set(report) != expected_keys:
        raise RepairError("audit report fields are invalid")
    if (
        report.get("schema_version") != SCHEMA_VERSION
        or report.get("mode") != "audit"
        or report.get("status") != "ok"
        or report.get("backup_manifest") is not None
    ):
        raise RepairError("unsupported or non-audit report artifact")
    if report.get("root_fingerprint") != _root_fingerprint(vault):
        raise RepairError("audit report root fingerprint does not match this invocation")
    if not isinstance(report.get("candidates"), list):
        raise RepairError("audit report candidates are invalid")
    if not isinstance(report.get("diagnostics"), list):
        raise RepairError("audit report diagnostics are invalid")
    stale_pages = report.get("stale_pages")
    if not isinstance(stale_pages, list) or any(
        not isinstance(value, str) for value in stale_pages
    ):
        raise RepairError("audit report stale-page request is invalid")
    if len({value.casefold() for value in stale_pages}) != len(stale_pages):
        raise RepairError("audit report stale-page request is duplicated")
    candidate_keys = {
        "id",
        "kind",
        "path_id",
        "action",
        "before_sha256",
        "after_sha256",
        "reason",
        "status",
        "metadata",
    }
    for item in [*report["candidates"], *report["diagnostics"]]:
        if (
            not isinstance(item, dict)
            or set(item) != candidate_keys
            or any(
                not isinstance(item.get(key), str)
                for key in ("id", "kind", "path_id", "action", "reason", "status")
            )
            or not _is_sha256(item.get("before_sha256"))
            or not _is_sha256(item.get("after_sha256"))
            or not isinstance(item.get("metadata"), dict)
        ):
            raise RepairError("audit report classification entry is invalid")
    if any(item["action"] not in MUTATING_ACTIONS for item in report["candidates"]):
        raise RepairError("audit report candidate is not actionable")
    if any(item["action"] in MUTATING_ACTIONS for item in report["diagnostics"]):
        raise RepairError("audit report diagnostic is unexpectedly actionable")
    if report.get("summary") != _summary(report["candidates"]):
        raise RepairError("audit report summary is inconsistent")


def _source_index(vault: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    index: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for namespace, directory, suffix in (
        ("daily", vault / "knowledge" / "daily", ".md"),
        ("feedback", vault / "knowledge" / "feedback", ".json"),
    ):
        for path in _safe_files(directory, suffix):
            path_id = _opaque_path_id(vault, path, namespace)
            data = path.read_bytes()
            found: list[dict[str, Any]]
            if namespace == "daily":
                candidate, _cleaned = _daily_analysis(data, path_id)
                found = [candidate] if candidate else []
            else:
                found = _feedback_candidates(path, vault)
            index[path_id] = {
                "path": path,
                "rel": path.relative_to(vault).as_posix(),
                "namespace": namespace,
                "data": data,
            }
            candidates.extend(found)
    return index, candidates


def _load_audit_report(path: Path, vault: Path, state_root: Path) -> tuple[dict[str, Any], bytes]:
    from memory_state import decode_json_object_strict

    if _path_is_link_or_reparse(path) or not path.is_file():
        raise RepairError(f"audit report must be a non-link regular file: {path}")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RepairError("audit report must be a singly linked regular file")
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise RepairError("audit report exceeds byte limit")
    data = path.read_bytes()
    try:
        report = decode_json_object_strict(data, max_bytes=MAX_MANIFEST_BYTES)
    except (UnicodeError, ValueError, TypeError, RecursionError, MemoryError) as exc:
        raise RepairError(f"invalid audit report: {exc}") from exc
    _validate_audit_report(report, vault, state_root)
    return report, data


def _ensure_backup_parent(state_root: Path) -> Path:
    state_root = state_root.absolute()
    if not state_root.exists():
        state_root.mkdir(parents=True)
    if _path_is_link_or_reparse(state_root):
        raise RepairError(f"symlink or reparse state root is not allowed: {state_root}")
    run = state_root / "run"
    backups = run / "backups"
    _safe_mkdir(run, state_root)
    _safe_mkdir(backups, state_root)
    return backups


def _entry_paths(backup_dir: Path, rel: str, action: str) -> tuple[Path, Path]:
    backup = backup_dir / "files" / Path(rel)
    staged = (
        backup_dir / "staged" / "quarantine" / Path(rel)
        if action == "quarantine"
        else backup_dir / "staged" / Path(rel)
    )
    return backup, staged


def _identity_record(identity: Any) -> dict[str, int]:
    return {
        "device": int(identity.device),
        "inode": int(identity.inode),
        "size": int(identity.size),
        "mtime_ns": int(identity.mtime_ns),
        "ctime_ns": int(identity.ctime_ns),
        "mode": int(identity.mode),
        "file_attributes": int(identity.file_attributes),
        "nlink": int(identity.nlink),
    }


def _v4_source_records(
    vault: Path,
    wanted_path_ids: set[str],
) -> dict[str, dict[str, Any]]:
    from daily_log_append import MAX_DAILY_MARKER_SCAN_BYTES
    from feedback_capture import MAX_FEEDBACK_BYTES
    from memory_state import bounded_path_inventory
    from session_start_project_state import MAX_PROJECT_STATE_OWNERSHIP_CHARS
    from vault_editorial import MAX_ACTIVE_NOTE_BYTES, read_bounded_note_snapshot

    records: dict[str, dict[str, Any]] = {}
    zones = (
        (
            "daily",
            vault / "knowledge" / "daily",
            "*.md",
            False,
            MAX_DAILY_MARKER_SCAN_BYTES,
        ),
        (
            "feedback",
            vault / "knowledge" / "feedback",
            "*.json",
            False,
            MAX_FEEDBACK_BYTES,
        ),
        (
            "note",
            vault / "knowledge" / "notes",
            "*.md",
            True,
            MAX_ACTIVE_NOTE_BYTES,
        ),
        (
            "project-state",
            vault / "knowledge" / "projects",
            "state.md",
            True,
            MAX_PROJECT_STATE_OWNERSHIP_CHARS,
        ),
    )
    for namespace, directory, pattern, recursive, max_bytes in zones:
        inventory_result = bounded_path_inventory(
            directory,
            pattern,
            MAX_REPAIR_INVENTORY_ENTRIES,
            recursive=recursive,
            kind="file",
        )
        if inventory_result.incomplete:
            raise RepairError(f"{namespace} source inventory is incomplete or unsafe")
        for path in inventory_result.paths:
            path_id = _opaque_path_id(vault, path, namespace)
            if path_id not in wanted_path_ids:
                continue
            try:
                snapshot = read_bounded_note_snapshot(path, max_bytes)
                relative = path.relative_to(vault).as_posix()
            except (OSError, ValueError) as exc:
                raise RepairError(f"unsafe {namespace} source snapshot: {path.name}") from exc
            if path_id in records:
                raise RepairError("source path identity collision")
            records[path_id] = {
                "path": path,
                "path_id": path_id,
                "rel": relative,
                "namespace": namespace,
                "data": snapshot.source_bytes,
                "sha256": snapshot.content_sha256,
                "size": snapshot.byte_size,
                "identity": _identity_record(snapshot.file_identity),
            }
    return records


def _manifest_sealed_bytes(manifest: dict[str, Any]) -> bytes:
    return _json_bytes({key: value for key, value in manifest.items() if key != "approved"})


def _v4_staging_files(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path_id": entry["path_id"],
            "source_staging_path": entry["source_staging_path"],
            "staged_sha256": entry["staged_sha256"],
            "staged_size": entry["staged_size"],
        }
        for entry in entries
    ]


def _new_v4_transaction(
    entries: list[dict[str, Any]],
    audit_digest: str,
    prepared_manifest_sha256: str,
    vault: Path,
    state_root: Path,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "vault_root": str(vault),
        "state_root": str(state_root),
        "root_fingerprint": _root_fingerprint(vault),
        "audit_report_sha256": audit_digest,
        "prepared_manifest_sha256": prepared_manifest_sha256,
        "status": "preparing",
        "staging_files": _v4_staging_files(entries),
        "staged_path_ids": [],
        "attempted_path_ids": [],
        "mutated_path_ids": [],
        "restored_path_ids": [],
        "results": [],
        "commit_error": None,
        "rollback_errors": [],
        "manual_recovery": [],
        "purged_path_ids": [],
    }


def _create_backup_locked(
    report: dict[str, Any],
    audit_bytes: bytes,
    vault: Path,
    state_root: Path,
) -> Path:
    _validate_audit_report(report, vault, state_root)
    expected = _expected_hashes(report["candidates"])
    authoritative = inventory(
        vault,
        state_root,
        stale_pages=tuple(report["stale_pages"]),
    )
    if (
        authoritative["candidates"] != report["candidates"]
        or authoritative["diagnostics"] != report["diagnostics"]
    ):
        raise RepairError(
            "authoritative classification of live sources does not match the audit report; "
            "fresh audit required because a source changed"
        )
    source_records = _v4_source_records(vault, set(expected))
    candidates_by_id = {
        candidate["path_id"]: candidate
        for candidate in report["candidates"]
        if candidate.get("action") in MUTATING_ACTIONS
    }
    if len(candidates_by_id) != len(expected):
        raise RepairError("multiple mutation instructions target one source")

    backups = _ensure_backup_parent(state_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = backups / stamp
    counter = 0
    while backup_dir.exists():
        counter += 1
        backup_dir = backups / f"{stamp}-{counter}"
    _safe_mkdir(backup_dir, backups)
    source_staging = backup_dir / "source-staging"
    _safe_mkdir(source_staging, backup_dir)
    entries = []
    for path_id in sorted(expected):
        candidate = candidates_by_id[path_id]
        source = source_records.get(path_id)
        if source is None:
            raise RepairError(f"audited source is missing: {path_id}")
        if source["sha256"] != expected[path_id]:
            raise RepairError(f"source changed since audit; fresh audit required: {path_id}")
        action = candidate["action"]
        staged = source_staging / f"{path_id}.source"
        postcondition = (
            {"kind": "sha256", "sha256": candidate["after_sha256"]}
            if action == "mark_handoff_unavailable"
            else {"kind": "absent"}
        )
        entries.append(
            {
                "path": source["rel"],
                "path_id": path_id,
                "action": action,
                "before_sha256": source["sha256"],
                "before_size": source["size"],
                "before_identity": source["identity"],
                "after_sha256": candidate["after_sha256"],
                "source_staging_path": staged.relative_to(backup_dir).as_posix(),
                "staged_sha256": source["sha256"],
                "staged_size": source["size"],
                "postcondition": postcondition,
            }
        )
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    audit_digest = _sha(audit_bytes)
    manifest_path = backup_dir / "manifest.json"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "prepared",
        "approved": False,
        "created_at": created_at,
        "vault_root": str(vault),
        "state_root": str(state_root),
        "root_fingerprint": _root_fingerprint(vault),
        "audit_report_sha256": audit_digest,
        "stale_pages": list(report["stale_pages"]),
        "files": entries,
        "candidates": report["candidates"],
        "diagnostics": report["diagnostics"],
    }
    prepared_manifest_sha256 = _sha(_manifest_sealed_bytes(manifest))
    outcome = _new_v4_transaction(
        entries,
        audit_digest,
        prepared_manifest_sha256,
        vault,
        state_root,
        created_at,
    )
    _persist_transaction(manifest_path, outcome)
    for entry in entries:
        source = source_records[entry["path_id"]]
        staged = backup_dir / Path(*PurePosixPath(entry["source_staging_path"]).parts)
        _durable_new_file(staged, source["data"], backup_dir)
        outcome["staged_path_ids"].append(entry["path_id"])
        _persist_transaction(manifest_path, outcome)
    _durable_new_file(manifest_path, _json_bytes(manifest), backup_dir)
    seal = {
        "schema_version": SCHEMA_VERSION,
        "sealed_manifest_sha256": prepared_manifest_sha256,
    }
    seal_path = backup_dir / "manifest.seal.json"
    _durable_new_file(seal_path, _json_bytes(seal), backup_dir)
    try:
        seal_path.chmod(0o400)
    except OSError:
        if os.name != "nt":
            raise
    _fsync_directory(backup_dir)
    validate_manifest(
        manifest_path,
        vault,
        state_root,
        audit_digest=_sha(audit_bytes),
        require_approved=False,
    )
    outcome["status"] = "prepared"
    _persist_transaction(manifest_path, outcome)
    return manifest_path


def create_backup(
    report: dict[str, Any],
    audit_bytes: bytes,
    vault: Path,
    state_root: Path,
) -> Path:
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)
        return _create_backup_locked(report, audit_bytes, vault, state_root)


def _manifest_location(manifest_path: Path, state_root: Path) -> Path:
    backups = state_root.absolute() / "run" / "backups"
    manifest_path = manifest_path.absolute()
    try:
        relative = manifest_path.relative_to(backups)
    except ValueError as exc:
        raise RepairError("manifest must be a direct run/backups transaction child") from exc
    if len(relative.parts) != 2 or relative.name != "manifest.json":
        raise RepairError("manifest must be a direct run/backups transaction child")
    _reject_link_components(manifest_path, backups)
    if manifest_path.name != "manifest.json" or not BACKUP_STAMP_RE.match(
        manifest_path.parent.name
    ):
        raise RepairError("manifest must be run/backups/<timestamp>/manifest.json")
    return _regular_file(manifest_path, backups)


def _validate_v3_manifest(
    manifest_path: Path,
    vault: Path,
    state_root: Path,
    *,
    audit_digest: str | None = None,
) -> dict[str, Any]:
    manifest_path = _manifest_location(manifest_path.absolute(), state_root)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairError(f"invalid backup manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise RepairError("unsupported backup manifest schema")
    if manifest.get("status") != "complete" or manifest.get("approved") is not True:
        raise RepairError("backup manifest is not complete and approved")
    if manifest.get("vault_root") != str(vault) or manifest.get("state_root") != str(state_root):
        raise RepairError("backup manifest roots do not match this invocation")
    if audit_digest is not None and manifest.get("audit_report_sha256") != audit_digest:
        raise RepairError("audit report digest does not match backup manifest")
    candidates = manifest.get("candidates")
    files = manifest.get("files")
    if not isinstance(candidates, list) or not isinstance(files, list):
        raise RepairError("backup manifest is missing candidates or files")
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("action") not in LEGACY_ACTIONS:
            raise RepairError("legacy manifest contains an unknown candidate action")
        if candidate["action"] == "propose_safe_api_delete" and (
            candidate.get("status") != "unsupported_safe_api"
            or candidate.get("before_sha256") != candidate.get("after_sha256")
            or candidate.get("metadata")
            != {"title_prefix": "memory-", "orphan_evidence": True}
        ):
            raise RepairError("legacy safe-API candidate status or metadata is invalid")
    expected = _expected_hashes(candidates, LEGACY_MUTATING_ACTIONS)
    contracts = _mutation_contracts(candidates, LEGACY_MUTATING_ACTIONS)
    file_ids = [entry.get("path_id") for entry in files if isinstance(entry, dict)]
    if len(files) != len(expected) or set(expected) != set(file_ids):
        raise RepairError("backup manifest file set is incomplete")
    backup_dir = manifest_path.parent
    for entry in files:
        if not isinstance(entry, dict):
            raise RepairError("invalid backup manifest file entry")
        rel = entry.get("path")
        if not isinstance(rel, str) or (vault / rel).absolute().is_relative_to(vault.absolute()) is False:
            raise RepairError(f"manifest source escapes vault: {rel}")
        path_id = entry.get("path_id")
        if not isinstance(path_id, str):
            raise RepairError("manifest source identity is invalid")
        expected_action, expected_after = contracts[path_id]
        path = Path(rel)
        allowed = (
            expected_action == "clean_daily"
            and path.parts[:2] == ("knowledge", "daily")
            and path.suffix.casefold() == ".md"
            and _opaque_path_id(vault, vault / path, "daily") == path_id
        ) or (
            expected_action == "quarantine"
            and path.parts[:2] == ("knowledge", "feedback")
            and path.suffix.casefold() == ".json"
            and _opaque_path_id(vault, vault / path, "feedback") == path_id
        )
        if not allowed:
            raise RepairError(f"manifest source is outside its action zone: {path_id}")
        if entry.get("action") != expected_action:
            raise RepairError(f"manifest action does not match audit report: {rel}")
        if entry.get("staged_sha256") != expected_after:
            raise RepairError(f"manifest staged hash does not match audit report: {rel}")
        for prefix, hash_key, size_key in (
            ("backup_path", "sha256", "size"),
            ("staged_path", "staged_sha256", "staged_size"),
        ):
            relative = entry.get(prefix)
            if not isinstance(relative, str):
                raise RepairError(f"invalid manifest {prefix}")
            artifact = _regular_file(backup_dir / relative, backup_dir)
            data = artifact.read_bytes()
            if _sha(data) != entry.get(hash_key) or len(data) != entry.get(size_key):
                raise RepairError(f"backup/staging hash mismatch (tampering): {relative}")
        if expected[path_id] != entry["sha256"]:
            raise RepairError(f"candidate and backup hash mismatch: {path_id}")
    return manifest


def _read_strict_json_file(path: Path, root: Path, description: str) -> dict[str, Any]:
    from memory_state import decode_json_object_strict

    artifact = _regular_file(path, root)
    metadata = artifact.stat()
    if metadata.st_size > MAX_MANIFEST_BYTES:
        raise RepairError(f"{description} exceeds byte limit")
    data = artifact.read_bytes()
    try:
        return decode_json_object_strict(data, max_bytes=MAX_MANIFEST_BYTES)
    except (UnicodeError, ValueError, TypeError, RecursionError, MemoryError) as exc:
        raise RepairError(f"invalid {description}: {exc}") from exc


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _v4_namespace_for_action(action: str) -> tuple[str, tuple[str, ...], str]:
    if action in {"delete_exact_duplicate_note", "delete_stale_note"}:
        return "note", ("knowledge", "notes"), ".md"
    if action == "delete_false_feedback":
        return "feedback", ("knowledge", "feedback"), ".json"
    if action == "delete_generated_daily":
        return "daily", ("knowledge", "daily"), ".md"
    if action == "mark_handoff_unavailable":
        return "project-state", ("knowledge", "projects"), ".md"
    raise RepairError(f"unknown manifest action: {action!r}")


def _validate_v4_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
    vault: Path,
    state_root: Path,
    *,
    audit_digest: str | None,
    require_approved: bool,
    require_staging: bool,
    missing_staging_path_ids: frozenset[str],
) -> dict[str, Any]:
    expected_manifest_keys = {
        "schema_version",
        "status",
        "approved",
        "created_at",
        "vault_root",
        "state_root",
        "root_fingerprint",
        "audit_report_sha256",
        "stale_pages",
        "files",
        "candidates",
        "diagnostics",
    }
    if set(manifest) != expected_manifest_keys:
        raise RepairError("v4 manifest fields are invalid")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "prepared":
        raise RepairError("unsupported or non-prepared v4 manifest")
    if not isinstance(manifest.get("approved"), bool):
        raise RepairError("v4 manifest approval is type-confused")
    if require_approved and manifest["approved"] is not True:
        raise RepairError("v4 manifest is not operator-approved")
    if manifest.get("vault_root") != str(vault) or manifest.get("state_root") != str(state_root):
        raise RepairError("v4 manifest roots do not match this invocation")
    if manifest.get("root_fingerprint") != _root_fingerprint(vault):
        raise RepairError("v4 manifest root fingerprint does not match")
    if not _is_sha256(manifest.get("audit_report_sha256")):
        raise RepairError("v4 manifest audit digest is invalid")
    if audit_digest is not None and manifest["audit_report_sha256"] != audit_digest:
        raise RepairError("audit report digest does not match v4 manifest")
    stale_pages = manifest.get("stale_pages")
    if not isinstance(stale_pages, list) or any(
        not isinstance(value, str) for value in stale_pages
    ):
        raise RepairError("v4 manifest stale-page request is invalid")
    if len({value.casefold() for value in stale_pages}) != len(stale_pages):
        raise RepairError("v4 manifest stale-page request is duplicated")

    candidates = manifest.get("candidates")
    diagnostics = manifest.get("diagnostics")
    files = manifest.get("files")
    if (
        not isinstance(candidates, list)
        or not isinstance(diagnostics, list)
        or not isinstance(files, list)
    ):
        raise RepairError("v4 manifest is missing candidates, diagnostics, or files")
    allowed_actions = MUTATING_ACTIONS | {
        "preserve",
        "review",
        "propose_safe_api_delete",
    }
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("action") not in allowed_actions:
            raise RepairError("v4 manifest contains an unknown candidate action")
        if candidate.get("action") not in MUTATING_ACTIONS:
            raise RepairError("v4 manifest candidate is not actionable")
    for diagnostic in diagnostics:
        if (
            not isinstance(diagnostic, dict)
            or diagnostic.get("action") not in allowed_actions - MUTATING_ACTIONS
        ):
            raise RepairError("v4 manifest contains an invalid report-only diagnostic")
    expected = _expected_hashes(candidates)
    contracts = _mutation_contracts(candidates)
    file_ids = [entry.get("path_id") for entry in files if isinstance(entry, dict)]
    if (
        len(files) != len(expected)
        or len(file_ids) != len(files)
        or len(set(file_ids)) != len(file_ids)
        or set(file_ids) != set(expected)
    ):
        raise RepairError("v4 manifest file set is incomplete or duplicated")
    if not missing_staging_path_ids.issubset(set(file_ids)):
        raise RepairError("v4 staging progress references an unknown source")

    entry_keys = {
        "path",
        "path_id",
        "action",
        "before_sha256",
        "before_size",
        "before_identity",
        "after_sha256",
        "source_staging_path",
        "staged_sha256",
        "staged_size",
        "postcondition",
    }
    identity_keys = {
        "device",
        "inode",
        "size",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "file_attributes",
        "nlink",
    }
    backup_dir = manifest_path.parent
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise RepairError("invalid v4 manifest file entry")
        path_id = entry.get("path_id")
        rel = entry.get("path")
        action = entry.get("action")
        if not isinstance(path_id, str) or path_id not in contracts:
            raise RepairError("v4 manifest source identity is invalid")
        if not isinstance(rel, str) or "\\" in rel:
            raise RepairError("v4 manifest source path is invalid")
        parsed = PurePosixPath(rel)
        if parsed.is_absolute() or parsed.as_posix() != rel or any(
            part in {"", ".", ".."} for part in parsed.parts
        ):
            raise RepairError("v4 manifest source path is unsafe")
        expected_action, expected_after = contracts[path_id]
        if action != expected_action or action not in MUTATING_ACTIONS:
            raise RepairError("v4 manifest action does not match audited candidate")
        namespace, prefix, suffix = _v4_namespace_for_action(action)
        if (
            parsed.parts[: len(prefix)] != prefix
            or parsed.suffix.casefold() != suffix
            or _opaque_path_id(vault, vault / Path(*parsed.parts), namespace) != path_id
        ):
            raise RepairError("v4 manifest source is outside its action zone")
        if action == "mark_handoff_unavailable" and (
            len(parsed.parts) != 4
            or parsed.parts[-1] != "state.md"
            or parsed.parts[-2].casefold() == "_template"
        ):
            raise RepairError("v4 project-state target is invalid")
        if action != "mark_handoff_unavailable" and len(parsed.parts) < 3:
            raise RepairError("v4 manifest source path is incomplete")
        before_sha = entry.get("before_sha256")
        after_sha = entry.get("after_sha256")
        if (
            not _is_sha256(before_sha)
            or not _is_sha256(after_sha)
            or before_sha != expected[path_id]
            or after_sha != expected_after
            or entry.get("staged_sha256") != before_sha
        ):
            raise RepairError("v4 manifest source or staged hash is inconsistent")
        before_size = entry.get("before_size")
        staged_size = entry.get("staged_size")
        if (
            isinstance(before_size, bool)
            or not isinstance(before_size, int)
            or before_size < 0
            or staged_size != before_size
        ):
            raise RepairError("v4 manifest source size is invalid")
        identity = entry.get("before_identity")
        if (
            not isinstance(identity, dict)
            or set(identity) != identity_keys
            or any(isinstance(value, bool) or not isinstance(value, int) for value in identity.values())
            or identity["size"] != before_size
            or identity["nlink"] != 1
        ):
            raise RepairError("v4 manifest source identity is invalid")
        expected_postcondition = (
            {"kind": "sha256", "sha256": after_sha}
            if action == "mark_handoff_unavailable"
            else {"kind": "absent"}
        )
        if entry.get("postcondition") != expected_postcondition:
            raise RepairError("v4 manifest postcondition is invalid")
        staging_rel = entry.get("source_staging_path")
        expected_staging_rel = f"source-staging/{path_id}.source"
        if staging_rel != expected_staging_rel:
            raise RepairError("v4 source staging path is invalid")
        staging_path = backup_dir / Path(*PurePosixPath(staging_rel).parts)
        if require_staging and os.path.lexists(staging_path):
            staged = _regular_file(staging_path, backup_dir).read_bytes()
            if _sha(staged) != before_sha or len(staged) != before_size:
                raise RepairError("v4 source staging hash mismatch (tampering)")
        elif require_staging and path_id not in missing_staging_path_ids:
            raise RepairError("v4 source staging artifact is missing")
        elif not require_staging and os.path.lexists(staging_path):
            raise RepairError("committed v4 transaction retains source staging bytes")

    staging_root = backup_dir / "source-staging"
    if os.path.lexists(staging_root):
        _reject_link_components(staging_root, backup_dir)
        if not staging_root.is_dir():
            raise RepairError("v4 source staging root is invalid")
        expected_staging = {
            Path(*PurePosixPath(entry["source_staging_path"]).parts)
            for entry in files
        }
        actual_staging = {
            path.relative_to(backup_dir) for path in staging_root.iterdir()
        }
        if not actual_staging.issubset(expected_staging):
            raise RepairError("v4 source staging inventory is invalid")
        if not require_staging:
            raise RepairError("committed v4 transaction retains source staging bytes")

    seal_path = manifest_path.with_name("manifest.seal.json")
    seal = _read_strict_json_file(seal_path, backup_dir, "v4 manifest seal")
    if set(seal) != {"schema_version", "sealed_manifest_sha256"} or seal.get(
        "schema_version"
    ) != SCHEMA_VERSION:
        raise RepairError("v4 manifest seal fields are invalid")
    if seal.get("sealed_manifest_sha256") != _sha(_manifest_sealed_bytes(manifest)):
        raise RepairError("v4 manifest seal mismatch")
    return manifest


def validate_manifest(
    manifest_path: Path,
    vault: Path,
    state_root: Path,
    *,
    audit_digest: str | None = None,
    require_approved: bool = True,
    require_staging: bool = True,
    missing_staging_path_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    located = _manifest_location(manifest_path.absolute(), state_root)
    parsed = _read_strict_json_file(located, located.parent, "repair manifest")
    schema = parsed.get("schema_version")
    if schema == LEGACY_SCHEMA_VERSION:
        if not require_staging:
            raise RepairError("legacy manifests always retain their recovery artifacts")
        return _validate_v3_manifest(
            located,
            vault,
            state_root,
            audit_digest=audit_digest,
        )
    if schema != SCHEMA_VERSION:
        raise RepairError("unsupported repair manifest schema")
    return _validate_v4_manifest(
        located,
        parsed,
        vault,
        state_root,
        audit_digest=audit_digest,
        require_approved=require_approved,
        require_staging=require_staging,
        missing_staging_path_ids=missing_staging_path_ids,
    )


@contextlib.contextmanager
def _writer_locks(state_root: Path):
    """Acquire every cooperative writer lock in one fixed deadlock-safe order."""
    from daily_log_append import _daily_lock
    from feedback_capture import feedback_writer_lock
    from memory_state import STATE_ROOT, advisory_file_lock, knowledge_publication_lock

    same_publication_root = os.path.normcase(str(state_root.absolute())) == os.path.normcase(
        str(STATE_ROOT.absolute())
    )
    publication_lock = (
        knowledge_publication_lock(timeout=30.0)
        if same_publication_root
        else advisory_file_lock(
            state_root / "run" / "knowledge-publication.lock",
            timeout=30.0,
            description="knowledge publication lock",
        )
    )
    with publication_lock:
        with _daily_lock(timeout=30.0, state_root=state_root):
            with feedback_writer_lock(state_root, timeout=30.0):
                with advisory_file_lock(
                    state_root / "run" / "project-state-claim.lock",
                    timeout=30.0,
                    description="project state claim",
                ):
                    yield


@contextlib.contextmanager
def _repair_writer_locks(state_root: Path):
    """Acquire the global repair lease before both cooperative writer locks."""
    from memory_state import advisory_file_lock

    state_root = state_root.absolute()
    if not state_root.exists():
        state_root.mkdir(parents=True)
    if _path_is_link_or_reparse(state_root):
        raise RepairError(f"symlink or reparse state root is not allowed: {state_root}")
    run = state_root / "run"
    _safe_mkdir(run, state_root)
    with advisory_file_lock(
        run / "repair-recovery.lock",
        timeout=30.0,
        description="repair/recovery lease",
    ):
        with _writer_locks(state_root):
            yield


def _quarantine_path(manifest_path: Path, rel: str) -> Path:
    backup_dir = manifest_path.parent
    destination = backup_dir / "quarantine" / Path(rel)
    _reject_link_components(destination, backup_dir)
    _resolved_containment(destination, backup_dir)
    return destination


def _restore_source(entry: dict[str, Any], manifest_path: Path, vault: Path) -> None:
    backup_dir = manifest_path.parent
    backup = _regular_file(backup_dir / entry["backup_path"], backup_dir)
    data = backup.read_bytes()
    if _sha(data) != entry["sha256"] or len(data) != entry["size"]:
        raise TransactionError(f"backup artifact changed before recovery: {entry['path_id']}")
    source = vault / entry["path"]
    _reject_link_components(source, vault)
    _atomic_write(source, data, vault)


def _persist_transaction(manifest_path: Path, outcome: dict[str, Any]) -> None:
    _atomic_write(
        manifest_path.parent / "transaction.json",
        _json_bytes(outcome),
        manifest_path.parent,
    )


def _validate_v4_transaction_journal(
    outcome: dict[str, Any],
    vault: Path,
    state_root: Path,
    manifest: dict[str, Any] | None = None,
) -> None:
    expected_keys = {
        "schema_version",
        "created_at",
        "vault_root",
        "state_root",
        "root_fingerprint",
        "audit_report_sha256",
        "prepared_manifest_sha256",
        "status",
        "staging_files",
        "staged_path_ids",
        "attempted_path_ids",
        "mutated_path_ids",
        "restored_path_ids",
        "results",
        "commit_error",
        "rollback_errors",
        "manual_recovery",
        "purged_path_ids",
    }
    if set(outcome) != expected_keys:
        raise TransactionError("v4 transaction journal fields are invalid")
    if (
        outcome.get("schema_version") != SCHEMA_VERSION
        or not isinstance(outcome.get("created_at"), str)
        or outcome.get("vault_root") != str(vault)
        or outcome.get("state_root") != str(state_root)
        or outcome.get("root_fingerprint") != _root_fingerprint(vault)
        or not _is_sha256(outcome.get("audit_report_sha256"))
        or not _is_sha256(outcome.get("prepared_manifest_sha256"))
    ):
        raise TransactionError("v4 transaction journal identity is invalid")
    allowed_statuses = {
        "preparing",
        "preparation_purge_pending",
        "preparation_aborted",
        "prepared",
        "aborted_precondition",
        "committing",
        "rolling_back",
        "rollback_complete_purge_pending",
        "rolled_back",
        "committed_pending_purge",
        "committed",
        "critical_manual_recovery",
        "critical_rollback_failed",
    }
    if outcome.get("status") not in allowed_statuses:
        raise TransactionError("v4 transaction journal status is invalid")
    staging_files = outcome.get("staging_files")
    if not isinstance(staging_files, list):
        raise TransactionError("v4 transaction staging ownership is invalid")
    staging_ids: list[str] = []
    for item in staging_files:
        if not isinstance(item, dict) or set(item) != {
            "path_id",
            "source_staging_path",
            "staged_sha256",
            "staged_size",
        }:
            raise TransactionError("v4 transaction staging ownership is invalid")
        path_id = item.get("path_id")
        size = item.get("staged_size")
        if (
            not _is_sha256(path_id)
            or item.get("source_staging_path") != f"source-staging/{path_id}.source"
            or not _is_sha256(item.get("staged_sha256"))
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise TransactionError("v4 transaction staging descriptor is invalid")
        staging_ids.append(path_id)
    if len(set(staging_ids)) != len(staging_ids):
        raise TransactionError("v4 transaction staging ownership is duplicated")
    progress: dict[str, list[str]] = {}
    for key in (
        "staged_path_ids",
        "attempted_path_ids",
        "mutated_path_ids",
        "restored_path_ids",
        "purged_path_ids",
    ):
        values = outcome.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            or len(set(values)) != len(values)
        ):
            raise TransactionError(f"v4 transaction {key} is invalid")
        progress[key] = values
    staged = progress["staged_path_ids"]
    attempted = progress["attempted_path_ids"]
    mutated = progress["mutated_path_ids"]
    restored = progress["restored_path_ids"]
    purged = progress["purged_path_ids"]
    if staged != staging_ids[: len(staged)]:
        raise TransactionError("v4 transaction staged progress is invalid")
    purge_order = [
        item["path_id"]
        for item in sorted(
            staging_files,
            key=lambda item: item["source_staging_path"],
        )
    ]
    if purged != purge_order[: len(purged)]:
        raise TransactionError("v4 transaction purge progress is invalid")
    if any(
        not isinstance(outcome.get(key), list)
        for key in ("results", "rollback_errors", "manual_recovery")
    ) or not (
        outcome.get("commit_error") is None
        or isinstance(outcome.get("commit_error"), str)
    ):
        raise TransactionError("v4 transaction result fields are invalid")
    results = outcome["results"]
    if any(
        not isinstance(result, dict)
        or set(result) != {
            "path_id",
            "action",
            "after_sha256",
            "postcondition",
        }
        for result in results
    ):
        raise TransactionError("v4 transaction results are invalid")

    error_path_ids: dict[str, list[str]] = {}
    for key in ("rollback_errors", "manual_recovery"):
        path_ids = []
        for issue in outcome[key]:
            if not isinstance(issue, dict) or not _is_sha256(issue.get("path_id")):
                raise TransactionError(f"v4 transaction {key} is invalid")
            path_ids.append(issue["path_id"])
        if len(set(path_ids)) != len(path_ids):
            raise TransactionError(f"v4 transaction {key} is duplicated")
        error_path_ids[key] = path_ids

    if manifest is not None:
        if (
            outcome["audit_report_sha256"] != manifest["audit_report_sha256"]
            or outcome["prepared_manifest_sha256"]
            != _sha(_manifest_sealed_bytes(manifest))
            or staging_files != _v4_staging_files(manifest["files"])
        ):
            raise TransactionError("v4 prepared manifest binding does not match its journal")
    status = outcome["status"]
    preparation_statuses = {
        "preparing",
        "preparation_purge_pending",
        "preparation_aborted",
    }
    if status in preparation_statuses:
        if (
            any((attempted, mutated, restored, results))
            or outcome["commit_error"] is not None
            or outcome["rollback_errors"]
            or outcome["manual_recovery"]
            or (status == "preparing" and purged)
            or (status == "preparation_aborted" and staged != purged[: len(staged)])
        ):
            raise TransactionError("v4 transaction preparation status progress is invalid")
        return
    if manifest is None:
        raise TransactionError("v4 transaction status requires its prepared manifest")
    if staged != staging_ids:
        raise TransactionError("v4 transaction prepared staging progress is invalid")

    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    action_ids = [entry["path_id"] for entry in entries]
    expected_results = [
        {
            "path_id": entry["path_id"],
            "action": entry["action"],
            "after_sha256": entry["after_sha256"],
            "postcondition": entry["postcondition"]["kind"],
        }
        for entry in entries
    ]
    if (
        attempted != action_ids[: len(attempted)]
        or mutated != action_ids[: len(mutated)]
        or len(mutated) > len(attempted)
        or len(attempted) - len(mutated) > 1
        or results != expected_results[: len(mutated)]
    ):
        raise TransactionError("v4 transaction action progress is invalid")

    reverse_attempted = list(reversed(attempted))
    restore_cursor = 0
    for path_id in restored:
        try:
            restore_cursor = reverse_attempted.index(path_id, restore_cursor) + 1
        except ValueError as exc:
            raise TransactionError("v4 transaction restore progress is invalid") from exc
    rollback_error_ids = error_path_ids["rollback_errors"]
    manual_recovery_ids = error_path_ids["manual_recovery"]
    if (
        rollback_error_ids != manual_recovery_ids
        or not set(rollback_error_ids).issubset(attempted)
    ):
        raise TransactionError("v4 transaction rollback error progress is invalid")
    reverse_mutated = list(reversed(mutated))
    restored_set = set(restored)
    rollback_error_set = set(rollback_error_ids)
    if restored != [path_id for path_id in reverse_mutated if path_id in restored_set]:
        raise TransactionError("v4 transaction restore order is invalid")
    if rollback_error_ids != [
        path_id for path_id in reverse_mutated if path_id in rollback_error_set
    ]:
        raise TransactionError("v4 transaction rollback error order is invalid")
    if status in {
        "rolling_back",
        "critical_manual_recovery",
        "critical_rollback_failed",
    } and (
        restored_set & rollback_error_set
        or restored_set | rollback_error_set != set(mutated)
    ):
        raise TransactionError("v4 transaction rollback accounting partition is invalid")

    commit_error = outcome["commit_error"]
    if status == "prepared":
        if any((attempted, mutated, restored, results, purged, rollback_error_ids)) or (
            commit_error is not None
        ):
            raise TransactionError("v4 transaction prepared status progress is invalid")
    elif status == "aborted_precondition":
        if any((attempted, mutated, restored, results, purged, rollback_error_ids)) or not (
            isinstance(commit_error, str) and commit_error
        ):
            raise TransactionError("v4 transaction aborted status progress is invalid")
    elif status == "committing":
        if (
            not attempted
            or restored
            or purged
            or rollback_error_ids
            or commit_error is not None
        ):
            raise TransactionError("v4 transaction committing status progress is invalid")
    elif status == "rolling_back":
        if purged or not (isinstance(commit_error, str) and commit_error):
            raise TransactionError("v4 transaction rollback status progress is invalid")
    elif status in {"rollback_complete_purge_pending", "rolled_back"}:
        if (
            rollback_error_ids
            or restored != reverse_mutated
            or not (isinstance(commit_error, str) and commit_error)
            or (status == "rolled_back" and purged != purge_order)
        ):
            raise TransactionError("v4 transaction rolled-back status progress is invalid")
    elif status == "critical_manual_recovery":
        if not rollback_error_ids or purged or not (
            isinstance(commit_error, str) and commit_error
        ):
            raise TransactionError("v4 transaction manual-recovery status progress is invalid")
    elif status == "critical_rollback_failed":
        if purged or not (isinstance(commit_error, str) and commit_error):
            raise TransactionError("v4 transaction critical rollback status progress is invalid")
    elif status in {"committed_pending_purge", "committed"}:
        if (
            attempted != action_ids
            or mutated != action_ids
            or results != expected_results
            or restored
            or rollback_error_ids
            or commit_error is not None
            or (status == "committed" and purged != purge_order)
        ):
            raise TransactionError("v4 transaction committed status progress is invalid")


def _order_v4_rollback_accounting(outcome: dict[str, Any]) -> None:
    reverse_mutated = list(reversed(outcome["mutated_path_ids"]))
    restored = set(outcome["restored_path_ids"])
    outcome["restored_path_ids"] = [
        path_id for path_id in reverse_mutated if path_id in restored
    ]
    for key in ("rollback_errors", "manual_recovery"):
        issues = {issue["path_id"]: issue for issue in outcome[key]}
        outcome[key] = [
            issues[path_id] for path_id in reverse_mutated if path_id in issues
        ]


def _record_v4_rollback_issue(
    outcome: dict[str, Any],
    path_id: str,
    issue: dict[str, Any],
) -> None:
    outcome["restored_path_ids"] = [
        existing for existing in outcome["restored_path_ids"] if existing != path_id
    ]
    for key in ("rollback_errors", "manual_recovery"):
        outcome[key] = [
            existing for existing in outcome[key] if existing.get("path_id") != path_id
        ]
        outcome[key].append(issue)
    _order_v4_rollback_accounting(outcome)


def _record_v4_rollback_success(outcome: dict[str, Any], path_id: str) -> None:
    for key in ("rollback_errors", "manual_recovery"):
        outcome[key] = [
            existing for existing in outcome[key] if existing.get("path_id") != path_id
        ]
    if path_id in outcome["mutated_path_ids"]:
        outcome["restored_path_ids"].append(path_id)
    _order_v4_rollback_accounting(outcome)


def _seed_v4_rollback_accounting(outcome: dict[str, Any]) -> None:
    accounted = set(outcome["restored_path_ids"]) | {
        issue["path_id"] for issue in outcome["rollback_errors"]
    }
    for path_id in outcome["mutated_path_ids"]:
        if path_id not in accounted:
            issue = {
                "path_id": path_id,
                "error": "TransactionError: rollback restoration pending",
            }
            outcome["rollback_errors"].append(issue)
            outcome["manual_recovery"].append(issue)
    _order_v4_rollback_accounting(outcome)


def _recover_v4_preparation_locked(
    transaction_dir: Path,
    outcome: dict[str, Any],
    vault: Path,
    state_root: Path,
) -> None:
    _validate_v4_transaction_journal(outcome, vault, state_root)
    staging_root = transaction_dir / "source-staging"
    manifest_path = transaction_dir / "manifest.json"
    if os.path.lexists(manifest_path):
        manifest = _read_strict_json_file(
            manifest_path,
            transaction_dir,
            "v4 preparation manifest",
        )
        if outcome["prepared_manifest_sha256"] != _sha(
            _manifest_sealed_bytes(manifest)
        ):
            raise TransactionError("v4 prepared manifest binding mismatch")
    if outcome["status"] == "preparation_aborted":
        if os.path.lexists(staging_root):
            raise TransactionError("aborted v4 preparation retains source staging")
        return
    if outcome["status"] not in {"preparing", "preparation_purge_pending"}:
        raise TransactionError("v4 preparation recovery status is invalid")
    staged_ids = set(outcome["staged_path_ids"])
    purged_ids = set(outcome["purged_path_ids"])
    if not os.path.lexists(staging_root):
        if staged_ids - purged_ids:
            raise TransactionError(
                "v4 durably staged artifact is missing without purge progress"
            )
        outcome["status"] = "preparation_aborted"
        _validate_v4_transaction_journal(outcome, vault, state_root)
        _persist_transaction(manifest_path, outcome)
        return
    _reject_link_components(staging_root, transaction_dir)
    if not staging_root.is_dir():
        raise TransactionError("v4 preparation staging root is invalid")
    owned = {
        Path(*PurePosixPath(item["source_staging_path"]).parts): item
        for item in outcome["staging_files"]
    }
    actual = {
        path.relative_to(transaction_dir)
        for path in staging_root.iterdir()
    }
    if not actual.issubset(owned):
        raise TransactionError("v4 preparation staging inventory contains an unknown path")
    required = {
        relative
        for relative, item in owned.items()
        if item["path_id"] in staged_ids - purged_ids
    }
    if not required.issubset(actual):
        raise TransactionError(
            "v4 durably staged artifact is missing without purge progress"
        )
    for relative in sorted(actual):
        item = owned[relative]
        artifact = _regular_file(transaction_dir / relative, transaction_dir)
        data = artifact.read_bytes()
        if _sha(data) != item["staged_sha256"] or len(data) != item["staged_size"]:
            raise TransactionError("v4 preparation staging bytes are not owned exactly")
    if outcome["status"] == "preparing":
        outcome["status"] = "preparation_purge_pending"
        outcome["purged_path_ids"] = []
        _persist_transaction(manifest_path, outcome)
    for relative in sorted(actual):
        item = owned[relative]
        if item["path_id"] not in outcome["purged_path_ids"]:
            outcome["purged_path_ids"].append(item["path_id"])
            _persist_transaction(manifest_path, outcome)
        artifact = transaction_dir / relative
        if os.path.lexists(artifact):
            _regular_file(artifact, transaction_dir).unlink()
            _fsync_directory(staging_root)
    staging_root.rmdir()
    _fsync_directory(transaction_dir)
    outcome["status"] = "preparation_aborted"
    _validate_v4_transaction_journal(outcome, vault, state_root)
    _persist_transaction(manifest_path, outcome)


def _artifact_bytes(entry: dict[str, Any], manifest_path: Path, key: str) -> bytes:
    artifact = _regular_file(manifest_path.parent / entry[key], manifest_path.parent)
    data = artifact.read_bytes()
    prefix = "staged" if key == "staged_path" else ""
    hash_key = f"{prefix}_sha256" if prefix else "sha256"
    size_key = f"{prefix}_size" if prefix else "size"
    if _sha(data) != entry[hash_key] or len(data) != entry[size_key]:
        raise TransactionError(f"recovery artifact changed: {entry['path_id']}")
    return data


def _manual_recovery_issue(
    entry: dict[str, Any],
    backup: bytes,
    staged: bytes,
    current: bytes | None,
    quarantine: bytes | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "path_id": entry["path_id"],
        "reason": reason,
        "backup_sha256": _sha(backup),
        "staged_sha256": _sha(staged),
        "current_sha256": _sha(current) if current is not None else None,
        "quarantine_sha256": _sha(quarantine) if quarantine is not None else None,
    }


def _remove_exact_quarantine(
    entry: dict[str, Any], manifest_path: Path, staged: bytes
) -> None:
    destination = _quarantine_path(manifest_path, entry["path"])
    if not destination.exists():
        return
    data = _regular_file(destination, manifest_path.parent).read_bytes()
    if data != staged:
        raise TransactionError(f"quarantine output diverged: {entry['path_id']}")
    destination.unlink()
    _fsync_directory(destination.parent)


def _feedback_recovery_path(source: Path, current: bytes) -> Path:
    return source.with_name(f"{source.stem}.recovered-{_sha(current)[:16]}{source.suffix}")


def _rollback_entry(
    entry: dict[str, Any],
    manifest_path: Path,
    vault: Path,
    outcome: dict[str, Any],
) -> None:
    backup = _artifact_bytes(entry, manifest_path, "backup_path")
    staged = _artifact_bytes(entry, manifest_path, "staged_path")
    source = vault / entry["path"]
    _reject_link_components(source, vault)
    current = _regular_file(source, vault).read_bytes() if source.exists() else None
    destination = _quarantine_path(manifest_path, entry["path"])
    quarantine = (
        _regular_file(destination, manifest_path.parent).read_bytes()
        if destination.exists()
        else None
    )
    if quarantine is not None and quarantine != staged:
        raise TransactionError(f"quarantine output diverged: {entry['path_id']}")

    previous = next(
        (
            merge
            for merge in outcome.setdefault("recovery_merges", [])
            if merge.get("path_id") == entry["path_id"]
        ),
        None,
    )
    if previous and previous.get("result_sha256") == (_sha(current) if current is not None else None):
        strategy = previous["strategy"]
        result = current
    elif current == backup:
        strategy = "already_restored"
        result = backup
    elif current == staged:
        strategy = "restore_backup"
        result = backup
    elif current is not None and current.startswith(staged) and entry["action"] == "clean_daily":
        strategy = "append_suffix"
        result = backup + current[len(staged) :]
    elif current is None and entry["action"] == "quarantine":
        strategy = "restore_deleted_quarantine"
        result = backup
    elif current is not None and entry["action"] == "quarantine":
        try:
            recreated = json.loads(current)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransactionError(f"feedback recreation requires manual recovery: {entry['path_id']}") from exc
        if not isinstance(recreated, dict):
            raise TransactionError(f"feedback recreation requires manual recovery: {entry['path_id']}")
        strategy = "feedback_recreation"
        result = backup
    else:
        raise TransactionError(f"source divergence requires manual recovery: {entry['path_id']}")

    merge = previous or {"path_id": entry["path_id"]}
    merge.update(
        {
            "strategy": strategy,
            "status": "planned",
            "current_sha256": _sha(current) if current is not None else None,
            "result_sha256": _sha(result) if result is not None else None,
        }
    )
    if strategy == "feedback_recreation" and current is not None and current != result:
        preserved = _feedback_recovery_path(source, current)
        merge["preserved_path"] = preserved.relative_to(vault).as_posix()
        merge["preserved_sha256"] = _sha(current)
    if previous is None:
        outcome["recovery_merges"].append(merge)
    _persist_transaction(manifest_path, outcome)

    if strategy == "feedback_recreation":
        preserved_rel = merge.get("preserved_path")
        preserved_hash = merge.get("preserved_sha256")
        if not isinstance(preserved_rel, str) or not isinstance(preserved_hash, str):
            raise TransactionError(f"feedback recovery journal is incomplete: {entry['path_id']}")
        preserved = vault / preserved_rel
        _reject_link_components(preserved, vault)
        if preserved.exists():
            if _sha(_regular_file(preserved, vault).read_bytes()) != preserved_hash:
                raise TransactionError(f"feedback recovery destination diverged: {entry['path_id']}")
        elif current is not None and _sha(current) == preserved_hash:
            _durable_new_file(preserved, current, vault)
        else:
            raise TransactionError(f"feedback recovery copy is missing: {entry['path_id']}")
        _persist_transaction(manifest_path, outcome)

    if current != result:
        if result == backup:
            _restore_source(entry, manifest_path, vault)
        elif result is not None:
            _atomic_write(source, result, vault)
    _remove_exact_quarantine(entry, manifest_path, staged)
    merge["status"] = "complete"
    _persist_transaction(manifest_path, outcome)


def _recover_v3_transaction_locked(
    manifest_path: Path,
    outcome: dict[str, Any],
    vault: Path,
    state_root: Path,
) -> None:
    if outcome.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise TransactionError(f"unsupported v3 recovery journal: {manifest_path.parent}")
    if outcome.get("status") in {"committed", "rolled_back"}:
        return
    manifest = validate_manifest(manifest_path, vault, state_root)
    entries = {entry["path_id"]: entry for entry in manifest["files"]}
    attempted = outcome.get("attempted_path_ids", [])
    if not isinstance(attempted, list) or any(path_id not in entries for path_id in attempted):
        raise TransactionError(f"invalid attempted set in recovery journal: {manifest_path}")
    outcome["status"] = "rolling_back"
    outcome.setdefault("manual_recovery", [])
    _persist_transaction(manifest_path, outcome)
    restored = []
    errors = []
    for path_id in reversed(attempted):
        try:
            _rollback_entry(entries[path_id], manifest_path, vault, outcome)
            restored.append(path_id)
        except Exception as exc:
            entry = entries[path_id]
            backup = _artifact_bytes(entry, manifest_path, "backup_path")
            staged = _artifact_bytes(entry, manifest_path, "staged_path")
            source = vault / entry["path"]
            current = source.read_bytes() if source.is_file() else None
            destination = _quarantine_path(manifest_path, entry["path"])
            quarantine = destination.read_bytes() if destination.is_file() else None
            issue = _manual_recovery_issue(
                entry, backup, staged, current, quarantine, _error_text(exc)
            )
            outcome["manual_recovery"] = [
                existing
                for existing in outcome["manual_recovery"]
                if existing.get("path_id") != path_id
            ]
            outcome["manual_recovery"].append(issue)
            errors.append(issue)
            _persist_transaction(manifest_path, outcome)
    outcome["restored_path_ids"] = restored
    outcome["status"] = "critical_manual_recovery" if errors else "rolled_back"
    _persist_transaction(manifest_path, outcome)
    if errors:
        raise TransactionError(f"transaction requires manual recovery: {manifest_path.parent}")


def _v4_purge_progress(outcome: dict[str, Any]) -> frozenset[str]:
    purged = outcome.get("purged_path_ids")
    if (
        not isinstance(purged, list)
        or any(not isinstance(path_id, str) for path_id in purged)
        or len(set(purged)) != len(purged)
    ):
        raise TransactionError("v4 staging purge progress is invalid")
    return frozenset(purged)


def _v4_rollback_postconditions(manifest: dict[str, Any], vault: Path) -> bool:
    for entry in manifest["files"]:
        try:
            _source, snapshot = _v4_read_current_snapshot(entry, vault)
        except PreMutationError:
            return False
        if (
            snapshot.content_sha256 != entry["before_sha256"]
            or snapshot.byte_size != entry["before_size"]
        ):
            return False
    return True


def _recover_v4_transaction_locked(
    manifest_path: Path,
    outcome: dict[str, Any],
    vault: Path,
    state_root: Path,
) -> None:
    if outcome.get("schema_version") != SCHEMA_VERSION:
        raise TransactionError(f"unsupported v4 recovery journal: {manifest_path.parent}")
    status = outcome.get("status")
    if status == "prepared":
        manifest = validate_manifest(
            manifest_path,
            vault,
            state_root,
            require_approved=False,
        )
        _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
        return
    if status in {"committed", "rolled_back"}:
        manifest = validate_manifest(
            manifest_path,
            vault,
            state_root,
            require_staging=False,
        )
        _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
        if status == "committed" and any(
            not _v4_entry_postcondition(entry, vault)[0]
            for entry in manifest["files"]
        ):
            raise TransactionError("committed v4 recovery postcondition failed")
        if status == "rolled_back" and not _v4_rollback_postconditions(manifest, vault):
            raise TransactionError("rolled-back v4 recovery postcondition failed")
        return
    purge_pending = status in {
        "committed_pending_purge",
        "rollback_complete_purge_pending",
    }
    missing_staging = _v4_purge_progress(outcome) if purge_pending else frozenset()
    manifest = validate_manifest(
        manifest_path,
        vault,
        state_root,
        missing_staging_path_ids=missing_staging,
    )
    _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
    if outcome.get("audit_report_sha256") != manifest["audit_report_sha256"]:
        raise TransactionError("v4 recovery journal audit digest mismatch")
    entries = {entry["path_id"]: entry for entry in manifest["files"]}
    attempted = outcome.get("attempted_path_ids", [])
    if (
        not isinstance(attempted, list)
        or len(set(attempted)) != len(attempted)
        or any(not isinstance(path_id, str) or path_id not in entries for path_id in attempted)
    ):
        raise TransactionError(f"invalid v4 attempted set: {manifest_path}")
    if status == "committed_pending_purge":
        if any(
            not _v4_entry_postcondition(entry, vault)[0]
            for entry in manifest["files"]
        ):
            raise TransactionError("pending v4 commit postcondition failed")
        _purge_v4_source_staging(manifest, manifest_path, outcome)
        outcome["status"] = "committed"
        _persist_transaction(manifest_path, outcome)
        return
    if status == "rollback_complete_purge_pending":
        if not _v4_rollback_postconditions(manifest, vault):
            raise TransactionError("pending v4 rollback postcondition failed")
        _purge_v4_source_staging(manifest, manifest_path, outcome)
        outcome["status"] = "rolled_back"
        _persist_transaction(manifest_path, outcome)
        return

    if status == "committing":
        outcome["commit_error"] = "TransactionError: interrupted v4 commit recovered"
    outcome["status"] = "rolling_back"
    outcome.setdefault("restored_path_ids", [])
    outcome.setdefault("rollback_errors", [])
    outcome.setdefault("manual_recovery", [])
    outcome.setdefault("purged_path_ids", [])
    _seed_v4_rollback_accounting(outcome)
    _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
    _persist_transaction(manifest_path, outcome)
    for path_id in reversed(attempted):
        entry = entries[path_id]
        try:
            restored = _v4_restore_entry(entry, manifest_path, vault)
            if restored and path_id not in outcome["mutated_path_ids"]:
                outcome["mutated_path_ids"].append(path_id)
                outcome["results"].append(
                    {
                        "path_id": path_id,
                        "action": entry["action"],
                        "after_sha256": entry["after_sha256"],
                        "postcondition": entry["postcondition"]["kind"],
                    }
                )
            _record_v4_rollback_success(outcome, path_id)
        except Exception as exc:
            if path_id not in outcome["mutated_path_ids"]:
                outcome["mutated_path_ids"].append(path_id)
                outcome["results"].append(
                    {
                        "path_id": path_id,
                        "action": entry["action"],
                        "after_sha256": entry["after_sha256"],
                        "postcondition": entry["postcondition"]["kind"],
                    }
                )
            current_sha = None
            source = vault / Path(*PurePosixPath(entry["path"]).parts)
            if os.path.lexists(source):
                try:
                    _path, snapshot = _v4_read_current_snapshot(entry, vault)
                    current_sha = snapshot.content_sha256
                except PreMutationError:
                    current_sha = "unsafe-or-unreadable"
            issue = {
                "path_id": path_id,
                "action": entry["action"],
                "reason": _error_text(exc),
                "staged_sha256": entry["staged_sha256"],
                "current_sha256": current_sha,
            }
            _record_v4_rollback_issue(outcome, path_id, issue)
        _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
        _persist_transaction(manifest_path, outcome)
    if outcome["rollback_errors"]:
        outcome["status"] = "critical_manual_recovery"
    else:
        if not _v4_rollback_postconditions(manifest, vault):
            outcome["status"] = "critical_rollback_failed"
            _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
            _persist_transaction(manifest_path, outcome)
            raise TransactionError("v4 rollback postcondition failed before purge")
        outcome["status"] = "rollback_complete_purge_pending"
        outcome["purged_path_ids"] = []
        _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
        _persist_transaction(manifest_path, outcome)
        _purge_v4_source_staging(manifest, manifest_path, outcome)
        outcome["status"] = "rolled_back"
    _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
    _persist_transaction(manifest_path, outcome)
    if outcome["rollback_errors"]:
        raise TransactionError(f"v4 transaction requires manual recovery: {manifest_path.parent}")


def _recover_incomplete_transactions_locked(vault: Path, state_root: Path) -> None:
    backups = state_root.absolute() / "run" / "backups"
    if not backups.exists():
        return
    _reject_link_components(backups, state_root.absolute())
    for child in sorted(backups.iterdir()):
        if _path_is_link_or_reparse(child) or not child.is_dir():
            raise RepairError(f"invalid transaction directory: {child}")
        transaction_path = child / "transaction.json"
        manifest_path = child / "manifest.json"
        if not transaction_path.exists():
            continue
        if BACKUP_STAMP_RE.fullmatch(child.name) is None:
            raise RepairError(f"invalid transaction directory name: {child.name}")
        outcome = _read_strict_json_file(
            transaction_path,
            child,
            "repair recovery journal",
        )
        if outcome.get("schema_version") == SCHEMA_VERSION and outcome.get("status") in {
            "preparing",
            "preparation_purge_pending",
            "preparation_aborted",
        }:
            _recover_v4_preparation_locked(child, outcome, vault, state_root)
            continue
        manifest_header = _read_strict_json_file(
            manifest_path,
            child,
            "repair recovery manifest",
        )
        schema = manifest_header.get("schema_version")
        if schema == LEGACY_SCHEMA_VERSION:
            _recover_v3_transaction_locked(manifest_path, outcome, vault, state_root)
        elif schema == SCHEMA_VERSION:
            _recover_v4_transaction_locked(manifest_path, outcome, vault, state_root)
        else:
            raise TransactionError(f"unsupported recovery manifest: {manifest_path}")


def recover_incomplete_transactions(vault: Path, state_root: Path) -> None:
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)


def _v4_staged_source(entry: dict[str, Any], manifest_path: Path) -> bytes:
    staged = _regular_file(
        manifest_path.parent / Path(*PurePosixPath(entry["source_staging_path"]).parts),
        manifest_path.parent,
    )
    data = staged.read_bytes()
    if _sha(data) != entry["staged_sha256"] or len(data) != entry["staged_size"]:
        raise PreMutationError(f"v4 staged source changed before use: {entry['path_id']}")
    return data


def _v4_read_current_snapshot(entry: dict[str, Any], vault: Path):
    from daily_log_append import MAX_DAILY_MARKER_SCAN_BYTES
    from feedback_capture import MAX_FEEDBACK_BYTES
    from session_start_project_state import MAX_PROJECT_STATE_OWNERSHIP_CHARS
    from vault_editorial import MAX_ACTIVE_NOTE_BYTES, read_bounded_note_snapshot

    limit = {
        "delete_exact_duplicate_note": MAX_ACTIVE_NOTE_BYTES,
        "delete_stale_note": MAX_ACTIVE_NOTE_BYTES,
        "delete_false_feedback": MAX_FEEDBACK_BYTES,
        "delete_generated_daily": MAX_DAILY_MARKER_SCAN_BYTES,
        "mark_handoff_unavailable": MAX_PROJECT_STATE_OWNERSHIP_CHARS,
    }[entry["action"]]
    source = vault / Path(*PurePosixPath(entry["path"]).parts)
    try:
        snapshot = read_bounded_note_snapshot(source, limit)
    except OSError as exc:
        raise PreMutationError(f"v4 source is unsafe or missing: {entry['path_id']}") from exc
    return source, snapshot


def _v4_current_snapshot(entry: dict[str, Any], vault: Path):
    source, snapshot = _v4_read_current_snapshot(entry, vault)
    if (
        snapshot.content_sha256 != entry["before_sha256"]
        or snapshot.byte_size != entry["before_size"]
        or _identity_record(snapshot.file_identity) != entry["before_identity"]
    ):
        raise PreMutationError(f"v4 source identity/hash drift: {entry['path_id']}")
    return source, snapshot


def _v4_revalidate_all_sources(
    manifest: dict[str, Any],
    vault: Path,
    state_root: Path,
) -> None:
    authoritative = inventory(
        vault,
        state_root,
        stale_pages=tuple(manifest["stale_pages"]),
    )
    if (
        authoritative["candidates"] != manifest["candidates"]
        or authoritative["diagnostics"] != manifest["diagnostics"]
    ):
        raise PreMutationError("v4 audit classification drift before mutation")
    sources = _v4_source_records(
        vault,
        {entry["path_id"] for entry in manifest["files"]},
    )
    for entry in manifest["files"]:
        source = sources.get(entry["path_id"])
        if (
            source is None
            or source["rel"] != entry["path"]
            or source["sha256"] != entry["before_sha256"]
            or source["size"] != entry["before_size"]
            or source["identity"] != entry["before_identity"]
        ):
            raise PreMutationError(f"v4 source drift before mutation: {entry['path_id']}")
        _v4_staged_source(entry, Path(manifest["_manifest_path"]))


def _v4_entry_postcondition(
    entry: dict[str, Any],
    vault: Path,
) -> tuple[bool, str]:
    source = vault / Path(*PurePosixPath(entry["path"]).parts)
    if entry["postcondition"]["kind"] == "absent":
        return not os.path.lexists(source), "absent"
    try:
        _source, snapshot = _v4_read_current_snapshot(entry, vault)
    except PreMutationError:
        return False, "sha256"
    return snapshot.content_sha256 == entry["after_sha256"], "sha256"


def _v4_commit_entry(
    entry: dict[str, Any],
    manifest_path: Path,
    vault: Path,
) -> None:
    staged = _v4_staged_source(entry, manifest_path)
    source, snapshot = _v4_current_snapshot(entry, vault)
    action = entry["action"]
    if action == "mark_handoff_unavailable":
        replacement = _project_state_replacement(staged)
        if replacement is None or _sha(replacement) != entry["after_sha256"]:
            raise PreMutationError(f"v4 project-state classification drift: {entry['path_id']}")
        if snapshot.source_bytes != staged:
            raise PreMutationError(f"v4 project-state CAS drift: {entry['path_id']}")
        _atomic_write(source, replacement, vault)
        source.chmod(stat.S_IMODE(snapshot.file_identity.mode))
        _fsync_directory(source.parent)
    else:
        source.unlink()
        _fsync_directory(source.parent)
    ok, _kind = _v4_entry_postcondition(entry, vault)
    if not ok:
        raise TransactionError(f"v4 action postcondition failed: {entry['path_id']}")


def _v4_restore_entry(
    entry: dict[str, Any],
    manifest_path: Path,
    vault: Path,
) -> bool:
    original = _v4_staged_source(entry, manifest_path)
    source = vault / Path(*PurePosixPath(entry["path"]).parts)
    current: bytes | None
    if os.path.lexists(source):
        try:
            _path, snapshot = _v4_read_current_snapshot(entry, vault)
            current = snapshot.source_bytes
        except PreMutationError as exc:
            raise TransactionError(
                f"v4 source divergence requires manual recovery: {entry['path_id']}"
            ) from exc
    else:
        current = None
    if current == original:
        return False
    if entry["action"] == "mark_handoff_unavailable":
        replacement = _project_state_replacement(original)
        if replacement is None or current != replacement:
            raise TransactionError(
                f"v4 recreated/diverged source requires manual recovery: {entry['path_id']}"
            )
        _atomic_write(source, original, vault)
        source.chmod(stat.S_IMODE(entry["before_identity"]["mode"]))
        _fsync_directory(source.parent)
        return True
    if current is not None:
        raise TransactionError(
            f"v4 recreated/diverged source requires manual recovery: {entry['path_id']}"
        )
    _durable_new_file(source, original, vault)
    source.chmod(stat.S_IMODE(entry["before_identity"]["mode"]))
    _fsync_directory(source.parent)
    return True


def _purge_v4_source_staging(
    manifest: dict[str, Any],
    manifest_path: Path,
    outcome: dict[str, Any],
) -> None:
    staging_root = manifest_path.parent / "source-staging"
    entries_by_path = {
        Path(*PurePosixPath(entry["source_staging_path"]).parts): entry
        for entry in manifest["files"]
    }
    expected = set(entries_by_path)
    path_ids = {entry["path_id"] for entry in manifest["files"]}
    purged = outcome.get("purged_path_ids")
    if (
        not isinstance(purged, list)
        or any(not isinstance(path_id, str) or path_id not in path_ids for path_id in purged)
        or len(set(purged)) != len(purged)
    ):
        raise TransactionError("v4 staging purge progress is invalid")
    if not os.path.lexists(staging_root):
        if set(purged) == path_ids:
            return
        raise TransactionError("v4 source staging disappeared before recorded purge")
    _reject_link_components(staging_root, manifest_path.parent)
    if not staging_root.is_dir():
        raise TransactionError("v4 source staging root is invalid before purge")
    actual = {
        path.relative_to(manifest_path.parent)
        for path in staging_root.iterdir()
    }
    if not actual.issubset(expected):
        raise TransactionError("v4 source staging inventory diverged before purge")
    allowed_missing = {
        relative
        for relative, entry in entries_by_path.items()
        if entry["path_id"] in purged
    }
    if not expected - actual <= allowed_missing:
        raise TransactionError("v4 source staging disappeared without recorded purge")
    for relative in sorted(expected):
        entry = entries_by_path[relative]
        path_id = entry["path_id"]
        artifact_path = manifest_path.parent / relative
        if path_id not in purged:
            if relative not in actual:
                raise TransactionError("v4 source staging disappeared before purge")
            purged.append(path_id)
            _persist_transaction(manifest_path, outcome)
        if os.path.lexists(artifact_path):
            _v4_staged_source(entry, manifest_path)
            artifact = _regular_file(artifact_path, manifest_path.parent)
            artifact.unlink()
            _fsync_directory(staging_root)
    _fsync_directory(staging_root)
    staging_root.rmdir()
    _fsync_directory(manifest_path.parent)


def _execute_v4_transaction(
    manifest: dict[str, Any],
    manifest_path: Path,
    vault: Path,
    state_root: Path,
) -> None:
    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    try:
        with _repair_writer_locks(state_root):
            _recover_incomplete_transactions_locked(vault, state_root)
            transaction_path = manifest_path.with_name("transaction.json")
            if not transaction_path.exists():
                raise TransactionError("v4 apply requires its prepared ownership journal")
            outcome = _read_strict_json_file(
                transaction_path,
                manifest_path.parent,
                "v4 transaction journal",
            )
            if outcome.get("status") == "committed":
                locked_manifest = validate_manifest(
                    manifest_path,
                    vault,
                    state_root,
                    require_staging=False,
                )
                _validate_v4_transaction_journal(outcome, vault, state_root, locked_manifest)
                if locked_manifest != manifest:
                    raise TransactionError("v4 manifest changed after commit")
                return
            if outcome.get("status") != "prepared":
                raise TransactionError("v4 transaction requires recovery before apply")
            locked_manifest = validate_manifest(manifest_path, vault, state_root)
            _validate_v4_transaction_journal(outcome, vault, state_root, locked_manifest)
            if locked_manifest != manifest:
                raise TransactionError("v4 manifest changed before transaction")
            manifest_with_path = {**manifest, "_manifest_path": str(manifest_path)}
            try:
                _v4_revalidate_all_sources(manifest_with_path, vault, state_root)
            except Exception as exc:
                outcome["status"] = "aborted_precondition"
                outcome["commit_error"] = _error_text(exc)
                _persist_transaction(manifest_path, outcome)
                raise
            touched: list[dict[str, Any]] = []
            try:
                for entry in entries:
                    touched.append(entry)
                    outcome["attempted_path_ids"].append(entry["path_id"])
                    outcome["status"] = "committing"
                    _persist_transaction(manifest_path, outcome)
                    _v4_commit_entry(entry, manifest_path, vault)
                    outcome["mutated_path_ids"].append(entry["path_id"])
                    outcome["results"].append(
                        {
                            "path_id": entry["path_id"],
                            "action": entry["action"],
                            "after_sha256": entry["after_sha256"],
                            "postcondition": entry["postcondition"]["kind"],
                        }
                    )
                    _persist_transaction(manifest_path, outcome)
            except Exception as commit_error:
                outcome["commit_error"] = _error_text(commit_error)
                rollback_entries = touched[:-1] if isinstance(commit_error, PreMutationError) else touched
                outcome["status"] = "rolling_back"
                _seed_v4_rollback_accounting(outcome)
                _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
                _persist_transaction(manifest_path, outcome)
                for entry in reversed(rollback_entries):
                    path_id = entry["path_id"]
                    try:
                        restored = _v4_restore_entry(entry, manifest_path, vault)
                        if restored and path_id not in outcome["mutated_path_ids"]:
                            outcome["mutated_path_ids"].append(path_id)
                            outcome["results"].append(
                                {
                                    "path_id": path_id,
                                    "action": entry["action"],
                                    "after_sha256": entry["after_sha256"],
                                    "postcondition": entry["postcondition"]["kind"],
                                }
                            )
                        _record_v4_rollback_success(outcome, path_id)
                    except Exception as rollback_error:
                        if path_id not in outcome["mutated_path_ids"]:
                            outcome["mutated_path_ids"].append(path_id)
                            outcome["results"].append(
                                {
                                    "path_id": path_id,
                                    "action": entry["action"],
                                    "after_sha256": entry["after_sha256"],
                                    "postcondition": entry["postcondition"]["kind"],
                                }
                            )
                        issue = {
                            "path_id": path_id,
                            "error": _error_text(rollback_error),
                        }
                        _record_v4_rollback_issue(outcome, path_id, issue)
                    _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
                    _persist_transaction(manifest_path, outcome)
                if outcome["rollback_errors"]:
                    outcome["status"] = "critical_manual_recovery"
                elif not _v4_rollback_postconditions(manifest, vault):
                    outcome["status"] = "critical_rollback_failed"
                else:
                    outcome["status"] = "rollback_complete_purge_pending"
                    outcome["purged_path_ids"] = []
                    _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
                    _persist_transaction(manifest_path, outcome)
                    _purge_v4_source_staging(manifest, manifest_path, outcome)
                    outcome["status"] = "rolled_back"
                _validate_v4_transaction_journal(outcome, vault, state_root, manifest)
                _persist_transaction(manifest_path, outcome)
                if outcome["rollback_errors"]:
                    severity = "manual recovery required"
                elif outcome["status"] == "critical_rollback_failed":
                    severity = "rollback recovery required"
                else:
                    severity = "rolled back"
                raise TransactionError(
                    f"v4 transaction {severity}: {_error_text(commit_error)}"
                ) from commit_error
            outcome["status"] = "committed_pending_purge"
            outcome["purged_path_ids"] = []
            _persist_transaction(manifest_path, outcome)
            _purge_v4_source_staging(manifest, manifest_path, outcome)
            outcome["status"] = "committed"
            _persist_transaction(manifest_path, outcome)
    except TransactionError:
        raise
    except RepairError:
        raise
    except Exception as exc:
        raise TransactionError(f"v4 transaction lock failure: {_error_text(exc)}") from exc


def _execute_transaction(
    manifest: dict[str, Any],
    manifest_path: Path,
    vault: Path,
    state_root: Path,
) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RepairError("mutating apply accepts only schema v4 manifests")
    _execute_v4_transaction(manifest, manifest_path, vault, state_root)


def apply_repair(
    audit_report: dict[str, Any],
    audit_bytes: bytes,
    vault: Path,
    state_root: Path,
    manifest_path: Path | None,
    *,
    backup_only: bool,
    stale_pages: tuple[str, ...] = (),
) -> dict[str, Any]:
    if backup_only and manifest_path is not None:
        raise RepairError("backup-only apply creates a new manifest and does not accept --manifest")
    if not backup_only and manifest_path is None:
        raise RepairError("mutating apply requires an explicit existing --manifest")
    if not backup_only and (
        _path_is_link_or_reparse(manifest_path) or not manifest_path.is_file()
    ):
        raise RepairError("mutating apply requires an explicit existing manifest file")
    _validate_audit_report(audit_report, vault, state_root)
    if list(stale_pages) != audit_report["stale_pages"]:
        raise RepairError("explicit stale-page list drifted from the audit request")
    audit_digest = _sha(audit_bytes)
    if backup_only:
        manifest_path = create_backup(audit_report, audit_bytes, vault, state_root)
    assert manifest_path is not None
    already_committed = False
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)
        located_manifest = _manifest_location(manifest_path.absolute(), state_root)
        manifest_header = _read_strict_json_file(
            located_manifest,
            located_manifest.parent,
            "repair manifest",
        )
        if not backup_only and manifest_header.get("schema_version") != SCHEMA_VERSION:
            raise RepairError("mutating apply accepts only schema v4 manifests")
        transaction_path = manifest_path.with_name("transaction.json")
        if not backup_only and transaction_path.exists():
            transaction = _read_strict_json_file(
                transaction_path,
                manifest_path.parent,
                "v4 transaction journal",
            )
            already_committed = (
                manifest_header.get("schema_version") == SCHEMA_VERSION
                and transaction.get("status") == "committed"
            )
        manifest = validate_manifest(
            manifest_path,
            vault,
            state_root,
            audit_digest=audit_digest,
            require_approved=not backup_only,
            require_staging=not already_committed,
        )
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise RepairError("mutating apply accepts only schema v4 manifests")
        if (
            manifest["candidates"] != audit_report["candidates"]
            or manifest.get("diagnostics") != audit_report["diagnostics"]
            or manifest.get("stale_pages") != audit_report["stale_pages"]
        ):
            raise RepairError("audit report content does not match backup manifest")
    if backup_only:
        return _report(
            "apply",
            "staging_prepared",
            vault,
            manifest["candidates"],
            manifest_path,
            stale_pages=tuple(manifest["stale_pages"]),
            diagnostics=manifest["diagnostics"],
        )
    if not already_committed:
        _execute_transaction(manifest, manifest_path, vault, state_root)
    applied = []
    for candidate in manifest["candidates"]:
        item = dict(candidate)
        if item["action"] in MUTATING_ACTIONS:
            item["status"] = "applied"
        applied.append(item)
    return _report(
        "apply",
        "applied",
        vault,
        applied,
        manifest_path,
        stale_pages=tuple(manifest["stale_pages"]),
        diagnostics=manifest["diagnostics"],
    )


def _verify_v3_repair_locked(
    vault: Path,
    state_root: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path, vault, state_root)
    entries = {entry["path_id"]: entry for entry in manifest["files"]}
    source_index, _authoritative = _source_index(vault)
    note_hashes = {
        _opaque_path_id(vault, path, "note"): _sha(path.read_bytes())
        for path in _safe_files(vault / "knowledge" / "notes", ".md")
        if path.name.casefold() not in {"readme.md", "index.md", "log.md"}
    }
    verified = []
    failures = []
    for candidate in manifest["candidates"]:
        item = dict(candidate)
        path_id = item["path_id"]
        action = item["action"]
        ok = True
        if action == "clean_daily":
            source = source_index.get(path_id)
            ok = source is not None and _sha(source["data"]) == item["after_sha256"]
        elif action == "quarantine":
            entry = entries[path_id]
            destination = _quarantine_path(manifest_path, entry["path"])
            quarantined = (
                _regular_file(destination, manifest_path.parent).read_bytes()
                if destination.exists()
                else None
            )
            ok = path_id not in source_index and quarantined is not None and _sha(
                quarantined
            ) == item["after_sha256"]
        elif action == "preserve":
            source = source_index.get(path_id)
            ok = source is not None and _sha(source["data"]) == item["before_sha256"]
        elif action == "review":
            members = item.get("metadata", {}).get("member_ids", [])
            hashes = sorted(note_hashes.get(member, "") for member in members)
            ok = bool(members) and all(hashes) and _sha(_json_bytes(hashes)) == item["before_sha256"]
        elif action == "propose_safe_api_delete":
            ok = (
                item.get("status") == "unsupported_safe_api"
                and item.get("before_sha256") == item.get("after_sha256")
                and item.get("metadata")
                == {"title_prefix": "memory-", "orphan_evidence": True}
            )
        else:
            raise RepairError(f"legacy verification action is unsupported: {action!r}")
        if not ok:
            failures.append(item["id"])
            item["status"] = "verification_failed"
        elif action in LEGACY_MUTATING_ACTIONS:
            item["status"] = "verified"
        verified.append(item)
    if failures:
        raise RepairError("verification failed for: " + ", ".join(failures))
    return _report("verify", "verified", vault, verified, manifest_path)


def _verify_v4_repair_locked(
    vault: Path,
    state_root: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    transaction_path = manifest_path.with_name("transaction.json")
    if not transaction_path.exists():
        raise RepairError("verify requires a committed v4 transaction")
    transaction = _read_strict_json_file(
        transaction_path,
        manifest_path.parent,
        "v4 transaction journal",
    )
    if transaction.get("schema_version") != SCHEMA_VERSION or transaction.get(
        "status"
    ) != "committed":
        raise RepairError("verify requires a committed v4 transaction")
    manifest = validate_manifest(
        manifest_path,
        vault,
        state_root,
        require_staging=False,
    )
    _validate_v4_transaction_journal(transaction, vault, state_root, manifest)
    if transaction.get("audit_report_sha256") != manifest["audit_report_sha256"]:
        raise RepairError("committed v4 transaction audit digest mismatch")
    expected_results = {
        (
            entry["path_id"],
            entry["action"],
            entry["after_sha256"],
            entry["postcondition"]["kind"],
        )
        for entry in manifest["files"]
    }
    results = transaction.get("results")
    if not isinstance(results, list):
        raise RepairError("committed v4 transaction results are missing")
    actual_results = {
        (
            result.get("path_id"),
            result.get("action"),
            result.get("after_sha256"),
            result.get("postcondition"),
        )
        for result in results
        if isinstance(result, dict)
    }
    if len(actual_results) != len(results) or actual_results != expected_results:
        raise RepairError("committed v4 transaction results are incomplete or invalid")

    from vault_editorial import select_active_notes

    try:
        selection = select_active_notes(vault / "knowledge" / "notes", root=vault)
    except OSError as exc:
        raise RepairError(f"active note verification failed: {exc}") from exc
    active_note_ids = {
        _opaque_path_id(vault, note.path, "note") for note in selection.notes
    }
    verified = []
    failures = []
    entries = {entry["path_id"]: entry for entry in manifest["files"]}
    for candidate in manifest["candidates"]:
        item = dict(candidate)
        action = item["action"]
        if action in MUTATING_ACTIONS:
            entry = entries[item["path_id"]]
            ok, _kind = _v4_entry_postcondition(entry, vault)
            if action == "delete_exact_duplicate_note":
                canonical_id = item.get("metadata", {}).get("canonical_path_id")
                ok = ok and canonical_id in active_note_ids and item["path_id"] not in active_note_ids
            elif action == "delete_stale_note":
                ok = ok and item["path_id"] not in active_note_ids
            if ok:
                item["status"] = "verified"
            else:
                item["status"] = "verification_failed"
                failures.append(item["id"])
        verified.append(item)
    if failures:
        raise RepairError("v4 verification failed for: " + ", ".join(failures))
    if os.path.lexists(manifest_path.parent / "source-staging"):
        raise RepairError("committed v4 transaction retains source staging")
    return _report(
        "verify",
        "verified",
        vault,
        verified,
        manifest_path,
        stale_pages=tuple(manifest["stale_pages"]),
        diagnostics=manifest["diagnostics"],
    )


def _verify_repair_locked(vault: Path, state_root: Path, manifest_path: Path) -> dict[str, Any]:
    header = _read_strict_json_file(
        _manifest_location(manifest_path.absolute(), state_root),
        manifest_path.parent,
        "repair manifest",
    )
    if header.get("schema_version") == LEGACY_SCHEMA_VERSION:
        return _verify_v3_repair_locked(vault, state_root, manifest_path)
    if header.get("schema_version") == SCHEMA_VERSION:
        return _verify_v4_repair_locked(vault, state_root, manifest_path)
    raise RepairError("unsupported repair manifest schema")


def verify_repair(vault: Path, state_root: Path, manifest_path: Path) -> dict[str, Any]:
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)
        return _verify_repair_locked(vault, state_root, manifest_path)


def _path_comparison_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _path_is_within(path: Path, root: Path) -> bool:
    path_key = _path_comparison_key(path)
    root_key = _path_comparison_key(root)
    try:
        return os.path.commonpath((path_key, root_key)) == root_key
    except ValueError:
        return False


def _preflight_output(
    path: Path,
    vault: Path,
    state_root: Path,
    input_paths: tuple[Path | None, ...],
) -> Path:
    output = path.absolute()
    parent = output.parent
    if not parent.is_dir():
        raise RepairError(f"output parent must be an existing directory: {parent}")
    anchor = Path(parent.anchor)
    _reject_link_components(parent, anchor)
    resolved_parent = parent.resolve(strict=True)
    resolved_output = resolved_parent / output.name
    for private_root, label in ((vault, "vault"), (state_root, "state")):
        if _path_is_within(output, private_root):
            raise RepairError(f"output must be outside the {label} root")
        if private_root.exists() and _path_is_within(
            resolved_output,
            private_root.resolve(strict=True),
        ):
            raise RepairError(f"output must not alias the {label} root")

    output_exists = os.path.lexists(output)
    if output_exists:
        if _path_is_link_or_reparse(output):
            raise RepairError(f"output must be a non-link file path: {output}")
        metadata = output.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RepairError(f"output must be a singly linked regular file: {output}")
    for input_path in input_paths:
        if input_path is None:
            continue
        candidate = input_path.absolute()
        if _path_comparison_key(output) == _path_comparison_key(candidate):
            raise RepairError("output must not alias an input artifact")
        if output_exists and os.path.lexists(candidate):
            try:
                same_file = os.path.samestat(output.stat(), candidate.stat())
            except OSError:
                same_file = False
            if same_file:
                raise RepairError("output must not hard-link or alias an input artifact")
    return output


def _write_output(path: Path, data: bytes) -> None:
    parent = path.absolute().parent
    if not parent.is_dir() or _path_is_link_or_reparse(parent):
        raise RepairError(f"output parent must be an existing non-link directory: {parent}")
    if _path_is_link_or_reparse(path) or (path.exists() and not path.is_file()):
        raise RepairError(f"output must be a non-link file path: {path}")
    _atomic_write(path.absolute(), data, parent)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("audit", "apply", "verify"))
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("LLM_WIKI_ROOT", ".")))
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--audit-report", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--backup-only", action="store_true")
    parser.add_argument("--sessions-file", type=Path)
    parser.add_argument("--stale-page", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lexical_vault = args.root.absolute()
        if _path_is_link_or_reparse(lexical_vault):
            raise RepairError(f"vault root must not be a symlink or reparse point: {lexical_vault}")
        vault = lexical_vault.resolve(strict=True)
        if not vault.is_dir():
            raise RepairError(f"vault root must be a directory: {vault}")
        state_root = (args.state_root or vault).absolute()
        output_path = (
            _preflight_output(
                args.output,
                vault,
                state_root,
                (args.audit_report, args.manifest, args.sessions_file),
            )
            if args.output is not None
            else None
        )
        if args.mode == "audit":
            if args.manifest or args.backup_only or args.audit_report:
                raise RepairError("audit accepts no apply artifacts")
            report = inventory(
                vault,
                state_root,
                args.sessions_file,
                tuple(args.stale_page),
            )
        elif args.mode == "apply":
            if args.audit_report is None:
                raise RepairError("apply requires --audit-report from a fresh audit")
            if args.backup_only and args.manifest is not None:
                raise RepairError("apply --backup-only creates a manifest and rejects --manifest")
            if not args.backup_only and args.manifest is None:
                raise RepairError("mutating apply requires an explicit existing --manifest")
            audit_report, audit_bytes = _load_audit_report(
                args.audit_report.absolute(), vault, state_root
            )
            report = apply_repair(
                audit_report,
                audit_bytes,
                vault,
                state_root,
                args.manifest.absolute() if args.manifest else None,
                backup_only=args.backup_only,
                stale_pages=tuple(args.stale_page),
            )
        else:
            if args.backup_only or args.manifest is None or args.stale_page:
                raise RepairError("verify requires --manifest and does not accept --backup-only")
            report = verify_repair(vault, state_root, args.manifest.absolute())
        output = _json_bytes(report)
        if output_path is not None:
            output_path = _preflight_output(
                output_path,
                vault,
                state_root,
                (args.audit_report, args.manifest, args.sessions_file),
            )
            _write_output(output_path, output)
        else:
            sys.stdout.buffer.write(output)
        return 0
    except (OSError, RepairError, ValueError) as exc:
        print(f"repair_installed_memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
