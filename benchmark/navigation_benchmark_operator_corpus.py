"""Bounded no-follow probing of an operator's private Python corpus that reports aggregates only."""

from __future__ import annotations

import io
import os
import stat
import sys
import tokenize
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
BENCHMARK_ROOT = Path(__file__).resolve().parent
for _path in (SCRIPTS_ROOT, BENCHMARK_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import windows_workspace as _windows_workspace  # noqa: E402
from code_intelligence import Capability  # noqa: E402
from code_navigation import (  # noqa: E402
    CodeNavigation,
    NavigationRequest,
    NavigationStatus,
)
from navigation_benchmark_deadlines import (  # noqa: E402
    BenchmarkTimeoutError,
    _check_run_deadline,
    _fresh_cleanup_deadline,
    _operation_deadline,
)
from navigation_benchmark_git import _require_qualified_identity  # noqa: E402
from navigation_benchmark_schema import (  # noqa: E402
    _is_count,
    load_manifest,
)
from pyright_profile import discover_pyright  # noqa: E402
from pyright_session import PyrightSession  # noqa: E402
from repository_scope import (  # noqa: E402
    RepositoryScope,
    resolve_repository_scope,
)

OPERATOR_MAX_SCANNED_ENTRIES = 10_000
OPERATOR_MAX_DEPTH = 32
OPERATOR_MAX_PYTHON_FILES = 20
OPERATOR_MAX_SOURCE_BYTES = 1024 * 1024
OPERATOR_READ_CHUNK_BYTES = 64 * 1024


class _OperatorTraversalError(RuntimeError):
    """Raised when the bounded operator traversal cannot complete safely."""


def _operator_file_state(info: object) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        0 if os.name == "nt" else info.st_ctime_ns,
    )


def _operator_link_or_reparse(info: object) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_plain_directory(info: object) -> None:
    if _operator_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("operator source parent must be a regular directory")


def _validate_operator_source_chain(path: Path, root: Path, deadline: float) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("operator source is outside operator root") from exc
    current = root
    while True:
        _check_run_deadline(deadline)
        _require_plain_directory(current.stat(follow_symlinks=False))
        if current == path.parent:
            return
        current /= path.parent.relative_to(current).parts[0]


def _expected_operator_state(source: Path) -> tuple[int, int, int, int, int, int]:
    before = source.stat(follow_symlinks=False)
    if _operator_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise PermissionError("operator source must be a regular file")
    if before.st_size > OPERATOR_MAX_SOURCE_BYTES:
        raise ValueError("operator source exceeds the byte limit")
    return _operator_file_state(before)


def _windows_source_state(handle: int) -> tuple[object, ...]:
    return (
        _windows_workspace.identity(handle, directory=False),
        _windows_workspace.file_size(handle),
        _windows_workspace.file_modified_time_ns(handle),
    )


def _open_operator_source(source: Path, flags: int) -> tuple[int, int | None, tuple[object, ...] | None]:
    """(descriptor, Windows handle, Windows state); the handle and state are None on POSIX."""
    if os.name != "nt":
        descriptor = os.open(source, flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        return descriptor, None, None
    import msvcrt

    handle = _windows_workspace.open_exclusive_readonly_source_file(source)
    try:
        state = _windows_source_state(handle)
        descriptor = msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _windows_workspace.close_handle(handle)
        raise
    return descriptor, handle, state


def _read_operator_chunks(descriptor: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= OPERATOR_MAX_SOURCE_BYTES:
        _check_run_deadline(deadline)
        chunk = os.read(descriptor, min(OPERATOR_READ_CHUNK_BYTES, OPERATOR_MAX_SOURCE_BYTES + 1 - total))
        _check_run_deadline(deadline)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > OPERATOR_MAX_SOURCE_BYTES:
        raise ValueError("operator source exceeds the byte limit")
    return b"".join(chunks)


def _require_source_unchanged(
    descriptor: int, expected_state: tuple, windows_handle: int | None, windows_state: tuple | None
) -> None:
    if _operator_file_state(os.fstat(descriptor)) != expected_state:
        raise PermissionError("operator source changed during read")
    if windows_handle is not None and _windows_source_state(windows_handle) != windows_state:
        raise PermissionError("operator source changed during read")


def _require_source_not_replaced(source: Path, expected_state: tuple) -> None:
    current = source.stat(follow_symlinks=False)
    if _operator_link_or_reparse(current) or _operator_file_state(current) != expected_state:
        raise PermissionError("operator source was replaced during read")


def _read_operator_source(path: Path, operator_root: Path, *, deadline: float) -> bytes:
    source = Path(os.path.abspath(os.fspath(path)))
    root = Path(os.path.abspath(os.fspath(operator_root)))
    _validate_operator_source_chain(source, root, deadline)
    _check_run_deadline(deadline)
    expected_state = _expected_operator_state(source)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    _check_run_deadline(deadline)
    descriptor, windows_handle, windows_state = _open_operator_source(source, flags)
    try:
        _check_run_deadline(deadline)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _operator_file_state(opened) != expected_state:
            raise PermissionError("operator source changed before open")
        content = _read_operator_chunks(descriptor, deadline)
        _require_source_unchanged(descriptor, expected_state, windows_handle, windows_state)
        _check_run_deadline(deadline)
        _require_source_not_replaced(source, expected_state)
        _validate_operator_source_chain(source, root, deadline)
        return content
    finally:
        os.close(descriptor)


def _definition_token_action(token, expect_name: bool) -> str | None:
    """`start` at def/class, `name` at the name that follows, `reset` at anything else."""
    if _starts_definition(token):
        return "start"
    if not expect_name:
        return None
    return _awaited_name_action(token)


def _awaited_name_action(token) -> str | None:
    if token.type == tokenize.NAME:
        return "name"
    if token.type not in {tokenize.NL, tokenize.INDENT}:
        return "reset"
    return None


def _starts_definition(token) -> bool:
    return token.type == tokenize.NAME and token.string in {"def", "class"}


def _definition_position(content: bytes, token, path: Path, root: Path) -> tuple[str, int, int]:
    line_bytes = content.splitlines()[token.start[0] - 1]
    prefix = line_bytes.decode("utf-8")[: token.start[1]].encode("utf-8")
    return path.relative_to(root).as_posix(), token.start[0], len(prefix)


def _operator_definition(
    path: Path,
    root: Path,
    *,
    operator_root: Path,
    deadline: float,
) -> tuple[str, int, int] | None:
    content = _read_operator_source(path, operator_root, deadline=deadline)
    expect_name = False
    for token in tokenize.tokenize(io.BytesIO(content).readline):
        action = _definition_token_action(token, expect_name)
        if action == "name":
            return _definition_position(content, token, path, root)
        if action is not None:
            expect_name = action == "start"
    return None


_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _require_operator_root(requested: Path) -> Path:
    try:
        metadata = requested.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator root is unavailable") from exc
    if requested.is_symlink() or getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG or not stat.S_ISDIR(metadata.st_mode):
        raise _OperatorTraversalError("operator root is not a regular directory")
    return requested.resolve(strict=True)


class _OperatorScan:
    """A bounded depth-first walk over an operator corpus."""

    def __init__(self, root: Path, deadline: float) -> None:
        self.root = root
        self.deadline = deadline
        self.files: list[Path] = []
        self.stack: list[tuple[Path, int]] = [(root, 0)]
        self.visited: set[tuple[int, int]] = set()
        self.scanned_entries = 0


def _directory_identity(current: Path) -> tuple[int, int]:
    try:
        metadata = current.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator directory is unavailable") from exc
    return metadata.st_dev, metadata.st_ino


def _scan_entries(scan: _OperatorScan, current: Path) -> list:
    try:
        with os.scandir(current) as iterator:
            entries = _counted_entries(scan, iterator)
    except OSError as exc:
        raise _OperatorTraversalError("operator directory cannot be scanned") from exc
    return entries


def _counted_entries(scan: _OperatorScan, iterator) -> list:
    entries = []
    for entry in iterator:
        _check_run_deadline(scan.deadline)
        scan.scanned_entries += 1
        if scan.scanned_entries > OPERATOR_MAX_SCANNED_ENTRIES:
            raise _OperatorTraversalError("operator traversal exceeds the entry limit")
        entries.append(entry)
    return entries


def _entry_metadata(entry):
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise _OperatorTraversalError("operator entry cannot be inspected") from exc


def _entry_is_link(entry, metadata) -> bool:
    return entry.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_FLAG)


def _classify_entry(entry, metadata) -> str:
    if _entry_is_link(entry, metadata):
        return "skip"
    return _unlinked_entry_kind(entry, metadata)


def _unlinked_entry_kind(entry, metadata) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode) and Path(entry.path).suffix == ".py":
        return "python"
    return "other"


def _child_directory(path: Path, depth: int) -> tuple[Path, int]:
    if depth + 1 > OPERATOR_MAX_DEPTH:
        raise _OperatorTraversalError("operator traversal exceeds the depth limit")
    return path, depth + 1


def _contained_python_file(path: Path, root: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _OperatorTraversalError("operator file escaped its root") from exc
    return candidate


def _visit_directory(scan: _OperatorScan, current: Path, depth: int) -> None:
    entries = _scan_entries(scan, current)
    child_directories: list[tuple[Path, int]] = []
    for entry in sorted(entries, key=lambda item: item.name):
        _check_run_deadline(scan.deadline)
        if _visit_entry(scan, entry, depth, child_directories):
            break
    scan.stack.extend(reversed(child_directories))


def _visit_entry(scan: _OperatorScan, entry, depth: int, child_directories: list[tuple[Path, int]]) -> bool:
    """Record one entry; True once the file bound is reached."""
    kind = _classify_entry(entry, _entry_metadata(entry))
    path = Path(entry.path)
    if kind == "directory":
        child_directories.append(_child_directory(path, depth))
        return False
    if kind != "python":
        return False
    scan.files.append(_contained_python_file(path, scan.root))
    return len(scan.files) >= OPERATOR_MAX_PYTHON_FILES


def _operator_python_files(
    operator_root: Path,
    *,
    deadline: float,
) -> tuple[Path, list[Path]]:
    root = _require_operator_root(Path(operator_root))
    scan = _OperatorScan(root, deadline)
    while scan.stack and len(scan.files) < OPERATOR_MAX_PYTHON_FILES:
        _check_run_deadline(deadline)
        current, depth = scan.stack.pop()
        identity = _directory_identity(current)
        if identity in scan.visited:
            continue
        scan.visited.add(identity)
        _visit_directory(scan, current, depth)
    return root, scan.files


class _ProbeTally:
    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.errors = 0
        self.available = True

    def fail(self) -> None:
        self.errors += 1
        self.available = False


def _probe_one_file(
    navigation: CodeNavigation,
    scope: RepositoryScope,
    checkout: Path,
    root: Path,
    path: Path,
    tally: _ProbeTally,
    deadline: float,
) -> None:
    query_deadline = _operation_deadline(deadline)
    definition = _operator_definition(path, checkout, operator_root=root, deadline=query_deadline)
    _check_run_deadline(deadline)
    if definition is None:
        return
    relative, line, character = definition
    tally.attempts += 1
    result = navigation.query(
        NavigationRequest(scope, Capability.DEFINITIONS, relative, line, character),
        deadline=query_deadline,
    )
    if result.status in {NavigationStatus.OK, NavigationStatus.PARTIAL}:
        tally.successes += 1
    _check_run_deadline(deadline)


def _probe_files(
    navigation: CodeNavigation,
    scope: RepositoryScope,
    root: Path,
    files: list[Path],
    tally: _ProbeTally,
    deadline: float,
) -> None:
    checkout = Path(scope.checkout_root)
    for path in files:
        try:
            _probe_one_file(navigation, scope, checkout, root, path, tally, deadline)
        except BenchmarkTimeoutError:
            raise
        except Exception:
            tally.fail()
            return


def _open_operator_navigation(root: Path, state_root: Path, deadline: float) -> tuple[CodeNavigation, RepositoryScope]:
    _check_run_deadline(deadline)
    scope = resolve_repository_scope(root)
    identity = discover_pyright(scope, state_root=state_root, deadline=_operation_deadline(deadline))
    _check_run_deadline(deadline)
    _require_qualified_identity(identity, load_manifest())
    session = PyrightSession(scope, identity, state_root=state_root)
    return CodeNavigation(scope, session, identity), scope


def _close_operator_navigation(navigation: CodeNavigation | None, tally: _ProbeTally) -> None:
    if navigation is None:
        return
    try:
        navigation.close(deadline=_fresh_cleanup_deadline())
    except Exception:
        tally.fail()
        try:
            navigation.close(deadline=_fresh_cleanup_deadline())
        except Exception:
            tally.errors += 1


def _probe_operator_corpus(
    operator_root: Path,
    state_root: Path,
    deadline: float,
) -> dict[str, object]:
    files: list[Path] = []
    tally = _ProbeTally()
    navigation: CodeNavigation | None = None
    try:
        root, files = _operator_python_files(operator_root, deadline=deadline)
        navigation, scope = _open_operator_navigation(root, state_root, deadline)
        _probe_files(navigation, scope, root, files, tally, deadline)
    except BenchmarkTimeoutError:
        raise
    except Exception:
        tally.fail()
    finally:
        _close_operator_navigation(navigation, tally)
    return {
        "available": tally.available,
        "python_files": len(files),
        "queries_attempted": tally.attempts,
        "queries_succeeded": tally.successes,
        "errors": tally.errors,
    }


def _counts_valid(value: dict, keys: set[str]) -> bool:
    return all(_is_count(value[key]) for key in keys - {"available"})


def _operator_metrics(value: object) -> dict[str, object]:
    keys = {"available", "python_files", "queries_attempted", "queries_succeeded", "errors"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("operator corpus probe returned a non-aggregate result")
    if not isinstance(value["available"], bool) or not _counts_valid(value, keys):
        raise ValueError("operator corpus aggregate metrics are invalid")
    return dict(value)
