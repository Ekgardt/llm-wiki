"""Audit and reversibly repair narrowly verified installed-memory noise.

Audit and verify are read-only. Apply requires an explicit audit artifact,
creates and validates a complete staged backup, then commits under the same
locks used by daily and feedback writers. Duplicate notes and service sessions
are always report-only. Cooperative writers and ordinary concurrency are in
scope. Malicious same-user path swapping is outside the threat model; identity,
containment, symlink, and reparse checks narrow but cannot eliminate that race.
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
TOOL_LINE_RE = re.compile(
    r"(?m)^[ \t]*- `\[(?P<time>\d{2}:\d{2}:\d{2})\] tool \| [^`\r\n]*`[ \t]*(?:\r?\n|$)"
)
IDLE_BLOCK_RE = re.compile(
    r"(?mis)^## \[(?P<time>\d{2}:\d{2}:\d{2})\] (?P<header>[^\r\n]*idle[^\r\n]*)\r?\n"
    r"(?P<body>.*?)(?=^## \[|\Z)"
)
EMPTY_BODY_MARKERS = {"(no body)", "(empty)", "- (no body)", "- (empty)"}
METADATA_PREFIXES = ("- tier:", "- trigger:", "- slug:", "- project root:")
BACKUP_STAMP_RE = re.compile(r"^\d{8}T\d{6}\.\d{6}Z(?:-\d+)?$")
MUTATING_ACTIONS = frozenset({"clean_daily", "quarantine"})


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
) -> dict[str, Any]:
    ordered = sorted(candidates, key=lambda c: (c["path_id"], c["kind"], c["id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "root_fingerprint": _root_fingerprint(vault),
        "backup_manifest": str(manifest) if manifest else None,
        "candidates": ordered,
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
    if not path.is_file():
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
    except UnicodeDecodeError as exc:
        raise RepairError(f"daily log is not UTF-8: {path_id}") from exc
    line_starts = [0]
    line_starts.extend(match.end() for match in re.finditer("\n", text))
    removals: list[tuple[int, int, str]] = []
    ranges: list[tuple[int, int]] = []
    idle_hashes: list[str] = []
    counts: Counter[str] = Counter()
    for match in TOOL_LINE_RE.finditer(text):
        removals.append((match.start(), match.end(), "empty_tool_breadcrumb"))
    for match in IDLE_BLOCK_RE.finditer(text):
        if _is_empty_idle_body(match["body"]):
            removals.append((match.start(), match.end(), "empty_idle_summary"))
            idle_hashes.append(_sha(match.group(0).encode("utf-8")))
    if not removals:
        return None, data
    for start, end, kind in removals:
        counts[kind] += 1
        start_line = bisect.bisect_right(line_starts, start)
        end_line = bisect.bisect_right(line_starts, max(start, end - 1))
        ranges.append((start_line, end_line))
    parts = []
    cursor = 0
    for start, end, _kind in sorted(removals):
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    cleaned = "".join(parts)
    cleaned_bytes = cleaned.encode("utf-8")
    merged_ranges = _merge_ranges(ranges)
    displayed_ranges = (
        merged_ranges if len(merged_ranges) <= 2 else [merged_ranges[0], merged_ranges[-1]]
    )
    metadata = {
        "empty_tool_breadcrumb_count": counts["empty_tool_breadcrumb"],
        "empty_idle_summary_count": counts["empty_idle_summary"],
        "line_range_count": len(merged_ranges),
        "line_ranges": displayed_ranges,
        "line_ranges_sha256": _sha(_json_bytes(merged_ranges)),
        "idle_blocks_sha256": _sha(_json_bytes(sorted(idle_hashes))),
    }
    candidate = _candidate(
        f"daily_noise:{path_id}",
        "daily_noise",
        path_id,
        "clean_daily",
        _sha(data),
        _sha(cleaned_bytes),
        "structurally verified empty daily-log records",
        metadata=metadata,
    )
    return candidate, cleaned_bytes


def _daily_candidates(path: Path, vault: Path) -> tuple[list[dict[str, Any]], bytes]:
    path_id = _opaque_path_id(vault, path, "daily")
    candidate, cleaned = _daily_analysis(path.read_bytes(), path_id)
    return ([candidate] if candidate else []), cleaned


def _feedback_candidates(path: Path, vault: Path) -> list[dict[str, Any]]:
    data = path.read_bytes()
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(record, dict):
        return []
    path_id = _opaque_path_id(vault, path, "feedback")
    digest = _sha(data)
    is_generated_idle = (
        record.get("trigger") == "opencode-idle"
        and record.get("source_role") != "user"
        and record.get("status") == "candidate"
    )
    if is_generated_idle:
        return [
            _candidate(
                f"false_feedback:{path_id}",
                "false_feedback",
                path_id,
                "quarantine",
                digest,
                digest,
                "legacy generated-idle feedback without user provenance",
                metadata={"classification": "generated_idle"},
            )
        ]
    classification = (
        "user_provenance"
        if record.get("source_role") == "user"
        or record.get("trigger") == "opencode-user-message"
        else "ambiguous_provenance"
    )
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


def _duplicate_candidates(notes: list[Path], vault: Path) -> list[dict[str, Any]]:
    records = []
    for path in notes:
        if path.name.casefold() in {"readme.md", "index.md", "log.md"}:
            continue
        data = path.read_bytes()
        records.append(
            {
                "path_id": _opaque_path_id(vault, path, "note"),
                "hash": _sha(data),
                "keys": _title_summary_keys(data, path.stem),
            }
        )
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_semantic: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_hash[record["hash"]].append(record)
        title, summary = record["keys"]
        if title:
            by_semantic[("title", title)].append(record)
        if summary:
            by_semantic[("summary", summary)].append(record)
    for digest, group in sorted(by_hash.items()):
        if len(group) < 2:
            continue
        member_ids = sorted(item["path_id"] for item in group)
        group_key = tuple(member_ids)
        group_id = f"group-{_sha(_json_bytes(member_ids))[:24]}"
        groups[group_key] = _duplicate_candidate(
            "exact_duplicate_note",
            group_id,
            "exact",
            1.0,
            member_ids,
            sorted(item["hash"] for item in group),
        )
    for (basis, value), group in sorted(by_semantic.items()):
        if len(group) < 2 or len({item["hash"] for item in group}) < 2:
            continue
        member_ids = sorted(item["path_id"] for item in group)
        group_key = tuple(member_ids)
        if group_key in groups:
            continue
        group_id = f"group-{_sha(_json_bytes(member_ids))[:24]}"
        score = 0.95 if basis == "title" else 0.9
        groups[group_key] = _duplicate_candidate(
            "semantic_duplicate_note",
            group_id,
            f"normalized_{basis}",
            score,
            member_ids,
            sorted(item["hash"] for item in group),
        )
    return list(groups.values())


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


def inventory(vault: Path, state_root: Path, sessions_file: Path | None = None) -> dict[str, Any]:
    vault = vault.resolve(strict=True)
    state_root = state_root.absolute()
    candidates: list[dict[str, Any]] = []
    for path in _safe_files(vault / "knowledge" / "daily", ".md"):
        found, _cleaned = _daily_candidates(path, vault)
        candidates.extend(found)
    for path in _safe_files(vault / "knowledge" / "feedback", ".json"):
        candidates.extend(_feedback_candidates(path, vault))
    candidates.extend(
        _duplicate_candidates(_safe_files(vault / "knowledge" / "notes", ".md"), vault)
    )
    candidates.extend(_session_candidates(sessions_file))
    return _report("audit", "ok", vault, candidates)


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


def _expected_hashes(candidates: list[dict[str, Any]]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for candidate in candidates:
        if candidate["action"] not in MUTATING_ACTIONS:
            continue
        path_id = candidate.get("path_id")
        if not isinstance(path_id, str) or len(path_id) < 32:
            raise RepairError("mutation candidate identity is invalid")
        previous = expected.setdefault(path_id, candidate["before_sha256"])
        if previous != candidate["before_sha256"]:
            raise RepairError(f"inconsistent candidate hashes for {path_id}")
    return expected


def _mutation_contracts(candidates: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    contracts: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        action = candidate.get("action")
        if action not in MUTATING_ACTIONS:
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
    if report.get("schema_version") != SCHEMA_VERSION or report.get("mode") != "audit":
        raise RepairError("unsupported or non-audit report artifact")
    if report.get("root_fingerprint") != _root_fingerprint(vault):
        raise RepairError("audit report root fingerprint does not match this invocation")
    if not isinstance(report.get("candidates"), list):
        raise RepairError("audit report candidates are invalid")


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


def _classification_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kinds = {"daily_noise", "false_feedback", "feedback_preserved"}
    return sorted(
        (candidate for candidate in candidates if candidate.get("kind") in kinds),
        key=lambda candidate: (candidate["path_id"], candidate["kind"], candidate["id"]),
    )


def _validate_authoritative_classification(
    report: dict[str, Any], authoritative: list[dict[str, Any]]
) -> None:
    if _classification_candidates(report["candidates"]) != _classification_candidates(
        authoritative
    ):
        raise RepairError(
            "authoritative classification of live sources does not match the audit report; "
            "fresh audit required because a source changed"
        )


def _load_audit_report(path: Path, vault: Path, state_root: Path) -> tuple[dict[str, Any], bytes]:
    if _path_is_link_or_reparse(path) or not path.is_file():
        raise RepairError(f"audit report must be a non-link regular file: {path}")
    data = path.read_bytes()
    try:
        report = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairError(f"invalid audit report: {exc}") from exc
    if not isinstance(report, dict):
        raise RepairError("audit report must contain a JSON object")
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


def _create_backup_locked(
    report: dict[str, Any],
    audit_bytes: bytes,
    vault: Path,
    state_root: Path,
) -> Path:
    _validate_audit_report(report, vault, state_root)
    expected = _expected_hashes(report["candidates"])
    source_data: dict[str, bytes] = {}
    staged_data: dict[str, bytes] = {}
    actions: dict[str, str] = {}
    after_hashes: dict[str, str] = {}
    source_index, authoritative = _source_index(vault)
    _validate_authoritative_classification(report, authoritative)
    for candidate in report["candidates"]:
        if candidate["action"] in MUTATING_ACTIONS:
            actions[candidate["path_id"]] = candidate["action"]
            after_hashes[candidate["path_id"]] = candidate["after_sha256"]
    for path_id in sorted(expected):
        source_record = source_index.get(path_id)
        if source_record is None:
            raise RepairError(f"audited source is missing: {path_id}")
        data = source_record["data"]
        if _sha(data) != expected[path_id]:
            raise RepairError(f"source changed since audit; fresh audit required: {path_id}")
        source_data[path_id] = data
        if actions[path_id] == "clean_daily":
            candidate, cleaned = _daily_analysis(data, path_id)
            if candidate is None or _sha(cleaned) != after_hashes[path_id]:
                raise RepairError(f"daily cleanup changed since audit: {path_id}")
            staged_data[path_id] = cleaned
        else:
            staged_data[path_id] = data

    backups = _ensure_backup_parent(state_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = backups / stamp
    counter = 0
    while backup_dir.exists():
        counter += 1
        backup_dir = backups / f"{stamp}-{counter}"
    _safe_mkdir(backup_dir, backups)
    _safe_mkdir(backup_dir / "files", backup_dir)
    _safe_mkdir(backup_dir / "staged", backup_dir)
    entries = []
    for path_id in sorted(expected):
        action = actions[path_id]
        rel = source_index[path_id]["rel"]
        backup, staged = _entry_paths(backup_dir, rel, action)
        _safe_mkdir(backup.parent, backup_dir)
        _safe_mkdir(staged.parent, backup_dir)
        _durable_new_file(backup, source_data[path_id], backup_dir)
        _durable_new_file(staged, staged_data[path_id], backup_dir)
        entries.append(
            {
                "path": rel,
                "path_id": path_id,
                "action": action,
                "sha256": expected[path_id],
                "size": len(source_data[path_id]),
                "backup_path": backup.relative_to(backup_dir).as_posix(),
                "staged_path": staged.relative_to(backup_dir).as_posix(),
                "staged_sha256": _sha(staged_data[path_id]),
                "staged_size": len(staged_data[path_id]),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "approved": True,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "vault_root": str(vault),
        "state_root": str(state_root),
        "audit_report_sha256": _sha(audit_bytes),
        "files": entries,
        "candidates": report["candidates"],
    }
    manifest_path = backup_dir / "manifest.json"
    _durable_new_file(manifest_path, _json_bytes(manifest), backup_dir)
    _fsync_directory(backup_dir)
    validate_manifest(
        manifest_path,
        vault,
        state_root,
        audit_digest=_sha(audit_bytes),
    )
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
    _reject_link_components(manifest_path, backups)
    if manifest_path.name != "manifest.json" or not BACKUP_STAMP_RE.match(
        manifest_path.parent.name
    ):
        raise RepairError("manifest must be run/backups/<timestamp>/manifest.json")
    return _regular_file(manifest_path, backups)


def validate_manifest(
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
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
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
    expected = _expected_hashes(candidates)
    contracts = _mutation_contracts(candidates)
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


@contextlib.contextmanager
def _writer_locks(state_root: Path):
    """Acquire writer locks in the sole allowed order: daily, then feedback."""
    from daily_log_append import _daily_lock
    from feedback_capture import feedback_writer_lock

    with _daily_lock(timeout=30.0, state_root=state_root):
        with feedback_writer_lock(state_root, timeout=30.0):
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


def _commit_staged_path(
    entry: dict[str, Any],
    manifest_path: Path,
    vault: Path,
) -> None:
    backup_dir = manifest_path.parent
    staged = _regular_file(backup_dir / entry["staged_path"], backup_dir)
    data = staged.read_bytes()
    if _sha(data) != entry["staged_sha256"] or len(data) != entry["staged_size"]:
        raise PreMutationError(f"staged artifact changed before use: {entry['path_id']}")
    source = _regular_file(vault / entry["path"], vault)
    if _sha(source.read_bytes()) != entry["sha256"]:
        raise PreMutationError(f"stale source before mutation: {entry['path_id']}")
    if entry["action"] == "clean_daily":
        _atomic_write(source, data, vault)
        return
    destination = _quarantine_path(manifest_path, entry["path"])
    _safe_mkdir(destination.parent, backup_dir)
    if destination.exists():
        raise RepairError(f"quarantine destination already exists: {destination}")
    _durable_new_file(destination, data, backup_dir)
    source.unlink()
    _fsync_directory(source.parent)


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
        transaction_path = _regular_file(transaction_path, child)
        try:
            outcome = json.loads(transaction_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransactionError(f"invalid recovery journal: {transaction_path}") from exc
        if not isinstance(outcome, dict) or outcome.get("schema_version") != SCHEMA_VERSION:
            raise TransactionError(f"unsupported recovery journal: {transaction_path}")
        if outcome.get("status") in {"committed", "rolled_back"}:
            continue
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


def recover_incomplete_transactions(vault: Path, state_root: Path) -> None:
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)


def _authoritative_sources(manifest: dict[str, Any], vault: Path) -> dict[str, bytes]:
    source_index, authoritative = _source_index(vault)
    _validate_authoritative_classification(
        {"candidates": manifest["candidates"]}, authoritative
    )
    sources: dict[str, bytes] = {}
    for entry in manifest["files"]:
        source_record = source_index.get(entry["path_id"])
        if source_record is None or source_record["rel"] != entry["path"]:
            raise RepairError(f"audited source identity is missing: {entry['path_id']}")
        data = source_record["data"]
        if _sha(data) != entry["sha256"]:
            raise RepairError(
                f"source changed before lock acquisition: {entry['path_id']}"
            )
        sources[entry["path_id"]] = data
    return sources


def _execute_transaction(
    manifest: dict[str, Any],
    manifest_path: Path,
    vault: Path,
    state_root: Path,
) -> None:
    entries = sorted(manifest["files"], key=lambda entry: entry["path"])
    outcome: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_report_sha256": manifest["audit_report_sha256"],
        "status": "prepared",
        "attempted_path_ids": [],
        "mutated_path_ids": [],
        "restored_path_ids": [],
        "attempted_paths": [],
        "mutated_paths": [],
        "restored_paths": [],
        "commit_error": None,
        "rollback_errors": [],
    }
    try:
        with _repair_writer_locks(state_root):
            _recover_incomplete_transactions_locked(vault, state_root)
            locked_manifest = validate_manifest(manifest_path, vault, state_root)
            if locked_manifest != manifest:
                raise TransactionError("backup manifest changed before transaction")
            transaction_path = manifest_path.parent / "transaction.json"
            if transaction_path.exists():
                existing = json.loads(
                    _regular_file(transaction_path, manifest_path.parent).read_text(
                        encoding="utf-8"
                    )
                )
                if existing.get("status") == "committed":
                    if existing.get("audit_report_sha256") != manifest["audit_report_sha256"]:
                        raise TransactionError("committed transaction audit digest mismatch")
                    return
            _persist_transaction(manifest_path, outcome)
            try:
                _authoritative_sources(manifest, vault)
            except Exception as exc:
                outcome["status"] = "aborted_stale_source"
                outcome["commit_error"] = _error_text(exc)
                _persist_transaction(manifest_path, outcome)
                raise
            touched: list[dict[str, Any]] = []
            try:
                for entry in entries:
                    touched.append(entry)
                    outcome["attempted_path_ids"].append(entry["path_id"])
                    outcome["attempted_paths"].append(entry["path"])
                    outcome["status"] = "committing"
                    _persist_transaction(manifest_path, outcome)
                    _commit_staged_path(entry, manifest_path, vault)
                    outcome["mutated_path_ids"].append(entry["path_id"])
                    outcome["mutated_paths"].append(entry["path"])
                    _persist_transaction(manifest_path, outcome)
                outcome["status"] = "committed"
                _persist_transaction(manifest_path, outcome)
                return
            except Exception as commit_error:
                outcome["commit_error"] = _error_text(commit_error)
                rollback_entries = touched
                if isinstance(commit_error, PreMutationError):
                    rollback_entries = touched[:-1]
                    outcome["attempted_path_ids"].pop()
                    outcome["attempted_paths"].pop()
                    _persist_transaction(manifest_path, outcome)
                outcome["status"] = "rolling_back"
                _persist_transaction(manifest_path, outcome)
                for entry in reversed(rollback_entries):
                    try:
                        _rollback_entry(entry, manifest_path, vault, outcome)
                        outcome["restored_path_ids"].append(entry["path_id"])
                        outcome["restored_paths"].append(entry["path"])
                    except Exception as rollback_error:
                        outcome["rollback_errors"].append(
                            {
                                "path_id": entry["path_id"],
                                "error": _error_text(rollback_error),
                            }
                        )
                outcome["status"] = (
                    "critical_rollback_failed"
                    if outcome["rollback_errors"]
                    else "rolled_back"
                )
                _persist_transaction(manifest_path, outcome)
                severity = "critical rollback failure" if outcome["rollback_errors"] else "rolled back"
                raise TransactionError(
                    f"transaction {severity}: {_error_text(commit_error)}; recovery artifacts: {manifest_path.parent}"
                ) from commit_error
    except TransactionError:
        raise
    except RepairError:
        raise
    except Exception as exc:
        raise TransactionError(f"transaction lock failure: {_error_text(exc)}") from exc


def apply_repair(
    audit_report: dict[str, Any],
    audit_bytes: bytes,
    vault: Path,
    state_root: Path,
    manifest_path: Path | None,
    *,
    backup_only: bool,
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
    audit_digest = _sha(audit_bytes)
    if backup_only:
        manifest_path = create_backup(audit_report, audit_bytes, vault, state_root)
    assert manifest_path is not None
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)
        manifest = validate_manifest(
            manifest_path,
            vault,
            state_root,
            audit_digest=audit_digest,
        )
        if manifest["candidates"] != audit_report["candidates"]:
            raise RepairError("audit report content does not match backup manifest")
    if backup_only:
        return _report(
            "apply",
            "backup_complete",
            vault,
            manifest["candidates"],
            manifest_path,
        )
    _execute_transaction(manifest, manifest_path, vault, state_root)
    applied = []
    for candidate in manifest["candidates"]:
        item = dict(candidate)
        if item["action"] in MUTATING_ACTIONS:
            item["status"] = "applied"
        applied.append(item)
    return _report("apply", "applied", vault, applied, manifest_path)


def _verify_repair_locked(vault: Path, state_root: Path, manifest_path: Path) -> dict[str, Any]:
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
        if not ok:
            failures.append(item["id"])
            item["status"] = "verification_failed"
        elif action in MUTATING_ACTIONS:
            item["status"] = "verified"
        verified.append(item)
    if failures:
        raise RepairError("verification failed for: " + ", ".join(failures))
    return _report("verify", "verified", vault, verified, manifest_path)


def verify_repair(vault: Path, state_root: Path, manifest_path: Path) -> dict[str, Any]:
    with _repair_writer_locks(state_root):
        _recover_incomplete_transactions_locked(vault, state_root)
        return _verify_repair_locked(vault, state_root, manifest_path)


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
        if args.mode == "audit":
            if args.manifest or args.backup_only or args.audit_report:
                raise RepairError("audit accepts no apply artifacts")
            report = inventory(vault, state_root, args.sessions_file)
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
            )
        else:
            if args.backup_only or args.manifest is None:
                raise RepairError("verify requires --manifest and does not accept --backup-only")
            report = verify_repair(vault, state_root, args.manifest.absolute())
        output = _json_bytes(report)
        if args.output:
            _write_output(args.output, output)
        else:
            sys.stdout.buffer.write(output)
        return 0
    except (OSError, RepairError, ValueError) as exc:
        print(f"repair_installed_memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
