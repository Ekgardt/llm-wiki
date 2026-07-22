"""Bounded live workspace revisions and content-proven deltas."""

from __future__ import annotations

import hashlib
import math
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reliable_memory import canonical_json_bytes
from repository_scope import RepositoryScope, sanitized_git_environment

PYTHON_CONFIG_NAMES = frozenset(
    {
        ".python-version",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pyproject.toml",
        "pyrightconfig.json",
        "setup.cfg",
        "tox.ini",
        "uv.lock",
    }
)
MAX_REVISION_FILES = 100_000
MAX_REVISION_BYTES = 2 * 1024 * 1024 * 1024
MAX_GIT_STATUS_BYTES = 16 * 1024 * 1024
GIT_STATUS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class RevisionEntry:
    path: str
    kind: str
    sha256: str | None
    size: int


@dataclass(frozen=True, slots=True)
class WorkspaceRevision:
    repository_id: str
    checkout_id: str
    git_head: str | None
    entries: tuple[RevisionEntry, ...]
    revision_sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceDelta:
    created: tuple[str, ...]
    changed: tuple[str, ...]
    renamed: tuple[tuple[str, str], ...]
    deleted: tuple[str, ...]
    configuration_changed: bool


def _check_stop(
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp or None")
    if cancelled is not None and not callable(cancelled):
        raise TypeError("cancelled must be callable or None")
    if cancelled is not None and cancelled():
        raise TimeoutError("workspace revision cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("workspace revision deadline reached")


def _normalized_path(raw: str) -> str:
    normalized = unicodedata.normalize("NFC", raw)
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("workspace revision path must be normalized relative POSIX text")
    return path.as_posix()


def _is_configuration(path: str) -> bool:
    return "/" not in path and (
        path in PYTHON_CONFIG_NAMES
        or (path.startswith("requirements") and path.endswith(".txt"))
    )


def _git_status(
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bytes:
    _check_stop(deadline, cancelled)
    command = [
        "git",
        "-C",
        root.as_posix(),
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        shell=False,
        env=sanitized_git_environment(),
    )
    stopped = threading.Event()
    stop_reason: list[str] = []
    local_deadline = time.monotonic() + GIT_STATUS_TIMEOUT_SECONDS
    effective_deadline = local_deadline if deadline is None else min(local_deadline, deadline)

    def monitor() -> None:
        while not stopped.is_set() and process.poll() is None:
            if cancelled is not None and cancelled():
                stop_reason.append("cancelled")
                process.kill()
                return
            remaining = effective_deadline - time.monotonic()
            if remaining <= 0:
                stop_reason.append("deadline")
                process.kill()
                return
            stopped.wait(min(0.01, remaining))

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    output = b""
    try:
        assert process.stdout is not None
        output = process.stdout.read(MAX_GIT_STATUS_BYTES + 1)
        if len(output) > MAX_GIT_STATUS_BYTES and process.poll() is None:
            process.kill()
        process.wait()
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        watcher.join(timeout=0.1)
    if stop_reason:
        raise TimeoutError(f"workspace revision {stop_reason[0]} during Git status")
    if len(output) > MAX_GIT_STATUS_BYTES:
        raise ValueError("Git status output exceeds the byte ceiling")
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
    return output


def _status_paths(output: bytes) -> list[tuple[str, str]]:
    records = output.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    result: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        marker = record[:1]
        try:
            if marker == b"1":
                raw_path = record.split(b" ", 8)[8]
                result.append((raw_path.decode("utf-8", errors="strict"), "modified"))
            elif marker == b"2":
                fields = record.split(b" ", 9)
                change = fields[8]
                raw_path = fields[9]
                index += 1
                original = records[index]
                if change.startswith(b"R"):
                    result.append((original.decode("utf-8", errors="strict"), "deleted"))
                result.append((raw_path.decode("utf-8", errors="strict"), "modified"))
            elif marker == b"u":
                raw_path = record.split(b" ", 10)[10]
                result.append((raw_path.decode("utf-8", errors="strict"), "modified"))
            elif marker == b"?":
                result.append((record[2:].decode("utf-8", errors="strict"), "untracked"))
            elif marker != b"!":
                raise ValueError("unknown Git status record")
        except (IndexError, UnicodeError) as exc:
            raise ValueError("malformed Git status output") from exc
        index += 1
    return result


def _relevant_files(
    root: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Iterator[Path]:
    for path in root.rglob("*"):
        _check_stop(deadline, cancelled)
        if (
            path.is_file()
            and ".git" not in path.relative_to(root).parts
            and path.suffix in {".py", ".pyi"}
        ):
            yield path
    for name in PYTHON_CONFIG_NAMES:
        _check_stop(deadline, cancelled)
        path = root / name
        if path.is_file():
            yield path
    for path in root.glob("requirements*.txt"):
        _check_stop(deadline, cancelled)
        if path.is_file():
            yield path


def _hash_file(
    path: Path,
    *,
    remaining_bytes: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str, int]:
    if path.stat().st_size > remaining_bytes:
        raise ValueError("workspace revision exceeds the byte ceiling")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            _check_stop(deadline, cancelled)
            chunk = source.read(min(1024 * 1024, remaining_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > remaining_bytes:
                raise ValueError("workspace revision exceeds the byte ceiling")
            digest.update(chunk)
    return digest.hexdigest(), size


def compute_workspace_revision(
    repository: RepositoryScope,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> WorkspaceRevision:
    """Compute a bounded content manifest for one live checkout."""
    _check_stop(deadline, cancelled)
    root = Path(repository.checkout_root)
    raw_entries: dict[str, tuple[str, Path | None]] = {}
    normalized_inputs: dict[str, str] = {}

    def add(raw: str, kind: str, path: Path | None) -> None:
        normalized = _normalized_path(raw)
        previous = normalized_inputs.get(normalized)
        if previous is not None and previous != raw:
            raise ValueError("workspace revision contains a Unicode normalization collision")
        normalized_inputs[normalized] = raw
        if len(raw_entries) >= MAX_REVISION_FILES and normalized not in raw_entries:
            raise ValueError("workspace revision exceeds the file-count ceiling")
        if _is_configuration(normalized) and path is not None:
            kind = "configuration"
        raw_entries[normalized] = (kind, path)

    if repository.git_common_dir is not None:
        for raw, status in _status_paths(
            _git_status(root, deadline=deadline, cancelled=cancelled)
        ):
            _check_stop(deadline, cancelled)
            path = root / PurePosixPath(raw)
            add(raw, "deleted" if status == "deleted" or not path.is_file() else status, path if path.is_file() else None)

    for path in _relevant_files(root, deadline=deadline, cancelled=cancelled):
        _check_stop(deadline, cancelled)
        raw = path.relative_to(root).as_posix()
        normalized = _normalized_path(raw)
        if normalized not in raw_entries:
            add(raw, "configuration" if _is_configuration(normalized) else "source", path)
        else:
            kind, existing = raw_entries[normalized]
            if existing is None:
                add(raw, kind, path)

    entries: list[RevisionEntry] = []
    total_bytes = 0
    for relative, (kind, path) in sorted(raw_entries.items()):
        _check_stop(deadline, cancelled)
        if path is None:
            entries.append(RevisionEntry(relative, "deleted", None, 0))
            continue
        sha256, size = _hash_file(
            path,
            remaining_bytes=MAX_REVISION_BYTES - total_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        total_bytes += size
        entries.append(RevisionEntry(relative, kind, sha256, size))

    values = {
        "repository_id": repository.repository_id,
        "checkout_id": repository.checkout_id,
        "git_head": repository.git_commit,
        "entries": [
            {"path": item.path, "kind": item.kind, "sha256": item.sha256, "size": item.size}
            for item in entries
        ],
    }
    return WorkspaceRevision(
        repository_id=repository.repository_id,
        checkout_id=repository.checkout_id,
        git_head=repository.git_commit,
        entries=tuple(entries),
        revision_sha256=hashlib.sha256(canonical_json_bytes(values)).hexdigest(),
    )


def diff_workspace_revisions(
    before: WorkspaceRevision, after: WorkspaceRevision
) -> WorkspaceDelta:
    """Return a deterministic delta with only unambiguous content renames paired."""
    if (before.repository_id, before.checkout_id) != (
        after.repository_id,
        after.checkout_id,
    ):
        raise ValueError("workspace revisions must describe the same checkout")
    before_entries = {entry.path: entry for entry in before.entries if entry.sha256 is not None}
    after_entries = {entry.path: entry for entry in after.entries if entry.sha256 is not None}
    created = set(after_entries) - set(before_entries)
    deleted = set(before_entries) - set(after_entries)
    changed = {
        path
        for path in set(before_entries) & set(after_entries)
        if before_entries[path].sha256 != after_entries[path].sha256
    }
    renames: list[tuple[str, str]] = []
    deleted_by_hash: dict[str, list[str]] = {}
    created_by_hash: dict[str, list[str]] = {}
    for path in deleted:
        deleted_by_hash.setdefault(str(before_entries[path].sha256), []).append(path)
    for path in created:
        created_by_hash.setdefault(str(after_entries[path].sha256), []).append(path)
    for digest in sorted(set(deleted_by_hash) & set(created_by_hash)):
        old = deleted_by_hash[digest]
        new = created_by_hash[digest]
        if len(old) == len(new) == 1:
            renames.append((old[0], new[0]))
            deleted.remove(old[0])
            created.remove(new[0])
    configuration_changed = any(
        _is_configuration(path) for path in created | changed | deleted
    )
    return WorkspaceDelta(
        created=tuple(sorted(created)),
        changed=tuple(sorted(changed)),
        renamed=tuple(sorted(renames)),
        deleted=tuple(sorted(deleted)),
        configuration_changed=configuration_changed,
    )
