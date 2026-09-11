"""Bounded Git-diff to active-Evidence-Graph impact analysis."""

from __future__ import annotations

import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from corpus_snapshot import (  # noqa: E402
    CorpusChanged,
    _open_descriptor_chain,
    _read_bounded_descriptor,
    _seal_path,
    _verify_seal,
)
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from repository_scope import sanitized_git_environment  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}
COMPARISONS = {"dirty", "worktree-index", "index-HEAD", "two-commits", "merge-base-branch"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}
TRAVERSED_EDGES = {
    "CALLS",
    "IMPORTS",
    "REFERENCES_SYMBOL",
    "DOCUMENTS",
    "DEFINES",
    "CHECKPOINT_CHANGED_FILE",
    "CHECKPOINT_RECORDED_DECISION",
}
CONFIRMED_CONFIDENCE = {"confirmed", "high"}
ZERO_OID = frozenset("0")
MAX_REVISION_LENGTH = 1024


class InvalidRevisionError(ValueError):
    """Raised when an impact endpoint is not a bounded verified commit."""


@dataclass(frozen=True)
class ImpactLimits:
    """Hard ceilings for one impact request."""

    max_files: int = 500
    max_blob_bytes: int = 4 * 1024 * 1024
    max_total_blob_bytes: int = 32 * 1024 * 1024
    max_graph_rows: int = 10_000
    max_symbols: int = 2_000
    max_depth: int = 8
    max_note_files: int = 2_000
    max_note_dirs: int = 256
    max_note_bytes: int = 2 * 1024 * 1024
    max_total_note_bytes: int = 32 * 1024 * 1024
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not all(_positive_integer(value) for value in self._integer_limits()):
            raise ValueError("impact limits must be positive integers")
        if not _positive_finite(self.timeout_seconds):
            raise ValueError("impact timeout must be a positive finite number")

    def _integer_limits(self) -> tuple[int, ...]:
        return (
            self.max_files,
            self.max_blob_bytes,
            self.max_total_blob_bytes,
            self.max_graph_rows,
            self.max_symbols,
            self.max_depth,
            self.max_note_files,
            self.max_note_dirs,
            self.max_note_bytes,
            self.max_total_note_bytes,
        )


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _limits_or_default(limits: ImpactLimits | None) -> ImpactLimits:
    if limits is None:
        return ImpactLimits()
    return limits


def _deadline_or_default(deadline: float | None, bounds: ImpactLimits, clock: Callable[[], float]) -> float:
    if deadline is not None:
        return deadline
    return clock() + bounds.timeout_seconds


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("impact analysis deadline reached")
    return remaining


def _check_impact_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None = None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("impact analysis cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("impact analysis deadline reached")


def _start_git(root: Path, arguments: list[str], stderr) -> subprocess.Popen:
    return subprocess.Popen(
        ["git", "-c", "core.fsmonitor=false", "-c", "diff.external=false", *arguments],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=sanitized_git_environment(),
        shell=False,
    )


def _read_to_ceiling(process: subprocess.Popen, max_bytes: int) -> bytes:
    assert process.stdout is not None
    stdout = process.stdout.read(max_bytes + 1)
    if len(stdout) > max_bytes:
        process.kill()
    process.wait()
    return stdout


def _stderr_head(stderr, limit: int = 1024) -> str:
    stderr.seek(0)
    return stderr.read(limit).decode("utf-8", errors="replace").strip()


def _git(root: Path, arguments: list[str], *, deadline: float, max_bytes: int) -> bytes:
    """Run Git without a shell and stop reading at the declared ceiling.

    Stderr is kept apart from the `-z` record stream: on a checkout with
    `core.autocrlf=true` Git prints an advisory line about line endings and
    still exits 0, and that line is not a diff record (research
    2026-09-11-git-warnings-are-not-diff-records.md).
    """
    with tempfile.TemporaryFile() as stderr:
        process = _start_git(root, arguments, stderr)
        timed_out = threading.Event()

        def stop_at_deadline() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(_remaining(deadline), stop_at_deadline)
        timer.daemon = True
        timer.start()
        try:
            stdout = _read_to_ceiling(process, max_bytes)
        finally:
            timer.cancel()
            _ensure_finished(process)
        _require_git_success(process, stdout, timed_out, max_bytes, _stderr_head(stderr))
    return stdout


def _ensure_finished(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()


def _require_git_success(
    process: subprocess.Popen, stdout: bytes, timed_out: threading.Event, max_bytes: int, stderr: str
) -> None:
    if timed_out.is_set():
        raise TimeoutError("Git impact command deadline reached")
    if len(stdout) > max_bytes:
        raise ValueError("Git impact output exceeds the read ceiling")
    _require_git_exit_zero(process, stdout, stderr)


def _require_git_exit_zero(process: subprocess.Popen, stdout: bytes, stderr: str) -> None:
    if process.returncode == 0:
        return
    detail = stderr or stdout[:1024].decode("utf-8", errors="replace").strip()
    raise ValueError(f"Git impact command failed: {detail or process.returncode}")


_UNSAFE_REVISION_CHARACTERS = frozenset("\0\r\n")


def _validate_revision(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not _option_safe_revision(value):
        raise InvalidRevisionError(
            f"{label} revision is required and must be bounded and option-safe"
        )
    return value


def _option_safe_revision(value: str) -> bool:
    if not value or len(value) > MAX_REVISION_LENGTH:
        return False
    return not value.startswith("-") and _UNSAFE_REVISION_CHARACTERS.isdisjoint(value)


def _resolve_revision(
    root: Path, value: str | None, label: str, *, deadline: float
) -> str:
    revision = _validate_revision(value, label)
    try:
        resolved = _git(
            root,
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            deadline=deadline,
            max_bytes=128,
        ).decode("ascii", errors="strict").strip()
    except (UnicodeError, ValueError) as exc:
        raise InvalidRevisionError(f"{label} revision is not a valid commit") from exc
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolved) is None:
        raise InvalidRevisionError(
            f"{label} revision did not resolve to a full commit object ID"
        )
    return resolved


_DIFF_COMMON = ("diff", "--raw", "--no-abbrev", "-z", "-M", "--no-ext-diff", "--no-textconv")


@dataclass(frozen=True)
class _DiffRequest:
    """One comparison and the endpoints it was given."""

    comparison: str
    base: str | None
    target: str | None
    branch: str | None
    root: Path
    deadline: float

    def resolve(self, value: str | None, label: str) -> str:
        return _resolve_revision(self.root, value, label, deadline=self.deadline)

    def require_no_endpoints(self) -> None:
        if any(value is not None for value in (self.base, self.target, self.branch)):
            raise ValueError(
                f"{self.comparison} comparison does not accept base, target, or branch"
            )


def _dirty_arguments(request: _DiffRequest) -> list[tuple[str, list[str], bool]]:
    request.require_no_endpoints()
    head = request.resolve("HEAD", "HEAD")
    return [
        ("index-HEAD", [*_DIFF_COMMON, "--cached", head, "--"], False),
        ("worktree-index", [*_DIFF_COMMON, "--"], True),
    ]


def _worktree_index_arguments(request: _DiffRequest) -> list[tuple[str, list[str], bool]]:
    request.require_no_endpoints()
    return [(request.comparison, [*_DIFF_COMMON, "--"], True)]


def _index_head_arguments(request: _DiffRequest) -> list[tuple[str, list[str], bool]]:
    request.require_no_endpoints()
    head = request.resolve("HEAD", "HEAD")
    return [(request.comparison, [*_DIFF_COMMON, "--cached", head, "--"], False)]


def _two_commit_arguments(request: _DiffRequest) -> list[tuple[str, list[str], bool]]:
    if request.branch is not None:
        raise ValueError("two-commits comparison does not accept branch")
    base = request.resolve(request.base, "base")
    target = request.resolve(request.target, "target")
    return [(request.comparison, [*_DIFF_COMMON, base, target, "--"], False)]


def _merge_base_arguments(request: _DiffRequest) -> list[tuple[str, list[str], bool]]:
    if request.target is not None:
        raise ValueError("merge-base-branch comparison does not accept target")
    base_value = request.resolve(request.base, "base")
    branch_value = request.resolve(request.branch, "branch")
    merge_base = _merge_base(request, base_value, branch_value)
    return [(request.comparison, [*_DIFF_COMMON, merge_base, branch_value, "--"], False)]


def _merge_base(request: _DiffRequest, base_value: str, branch_value: str) -> str:
    merge_base = _git(
        request.root,
        ["merge-base", "--", base_value, branch_value],
        deadline=request.deadline,
        max_bytes=4096,
    ).decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_base):
        raise ValueError("Git merge-base did not return an object ID")
    return merge_base


_DIFF_BUILDERS = {
    "dirty": _dirty_arguments,
    "worktree-index": _worktree_index_arguments,
    "index-HEAD": _index_head_arguments,
    "two-commits": _two_commit_arguments,
    "merge-base-branch": _merge_base_arguments,
}


def _diff_arguments(
    comparison: str,
    *,
    base: str | None,
    target: str | None,
    branch: str | None,
    root: Path,
    deadline: float,
) -> list[tuple[str, list[str], bool]]:
    builder = _DIFF_BUILDERS.get(comparison)
    if builder is None:
        raise ValueError(f"comparison must be one of: {', '.join(sorted(COMPARISONS))}")
    return builder(_DiffRequest(comparison, base, target, branch, root, deadline))


def _decode_path(value: bytes) -> str:
    return os.fsdecode(value).replace("\\", "/")


def _parse_raw_records(raw: bytes, comparison: str) -> list[dict]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    records = []
    index = 0
    while index < len(fields):
        record, index = _parse_raw_record(fields, index, comparison)
        records.append(record)
    return records


def _parse_raw_record(fields: list[bytes], index: int, comparison: str) -> tuple[dict, int]:
    """One record starting at `index`, and the index of the field after it."""
    parts = _raw_header_parts(fields[index])
    status_text = parts[4].decode("ascii", errors="strict")
    status = status_text[:1]
    path_count = 2 if status in {"R", "C"} else 1
    old_path, new_path = _record_paths(fields, index + 1, path_count)
    record = {
        "comparison": comparison,
        "status": status,
        "similarity": _similarity(status_text),
        "old_path": old_path,
        "new_path": new_path,
        "old_oid": parts[2].decode("ascii", errors="strict"),
        "new_oid": parts[3].decode("ascii", errors="strict"),
    }
    return record, index + 1 + path_count


def _raw_header_parts(header: bytes) -> list[bytes]:
    if not header.startswith(b":"):
        raise ValueError("malformed zero-delimited Git diff record")
    parts = header[1:].split()
    if len(parts) != 5:
        raise ValueError("malformed Git raw diff metadata")
    return parts


def _record_paths(fields: list[bytes], index: int, path_count: int) -> tuple[str, str]:
    if index + path_count > len(fields):
        raise ValueError("truncated zero-delimited Git pathname record")
    old_path = _decode_path(fields[index])
    if path_count == 1:
        return old_path, old_path
    return old_path, _decode_path(fields[index + 1])


def _similarity(status_text: str) -> int | None:
    digits = status_text[1:]
    if not digits.isdigit():
        return None
    return int(digits)


def _object_blob(
    root: Path,
    oid: str,
    *,
    deadline: float,
    limit: int,
) -> bytes | None:
    if not oid or set(oid) <= ZERO_OID:
        return None
    return _git(root, ["cat-file", "blob", oid], deadline=deadline, max_bytes=limit)


def _worktree_blob(root: Path, relative: str, limit: int) -> bytes | None:
    components = relative.split("/")
    if not components or any(part in {"", ".", ".."} for part in components):
        raise PermissionError("changed worktree path is not repository-relative")
    path = root.joinpath(*components)
    try:
        return read_stable_bytes(path, limit, label=f"changed worktree file {relative}")
    except FileNotFoundError:
        return None


def _capture_note_file(root: Path, path: Path, limit: int) -> bytes:
    """Capture one note through a root-anchored stable descriptor chain."""
    relative_parts = _note_relative_parts(root, path)
    seal = _seal_path(
        root,
        path,
        target_directory=False,
        max_components=len(relative_parts),
    )
    try:
        return _capture_sealed_note(seal, path, limit)
    except CorpusChanged as exc:
        raise PermissionError("impact note changed during capture") from exc


def _note_relative_parts(root: Path, path: Path) -> tuple[str, ...]:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("impact note path escapes the notes root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PermissionError("impact note path is not root-relative")
    return relative.parts


def _capture_sealed_note(seal, path: Path, limit: int) -> bytes:
    """Read the note and verify its seal while the descriptor is still open."""
    if os.name != "posix":
        content = read_stable_bytes(path, limit, label="impact note")
        _verify_seal(seal, changed_error=PermissionError)
        return content
    descriptor = _open_descriptor_chain(seal, changed_error=PermissionError)
    try:
        content = _read_bounded_descriptor(descriptor, limit)
        _verify_seal(seal, changed_error=PermissionError)
        return content
    finally:
        os.close(descriptor)


def collect_git_changes(
    root: Path = ROOT,
    *,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Collect NUL-safe diff records and their bounded old/new blobs."""
    bounds = _limits_or_default(limits)
    root = Path(root).resolve(strict=True)
    if comparison not in COMPARISONS:
        raise ValueError(f"comparison must be one of: {', '.join(sorted(COMPARISONS))}")
    collector = _ChangeCollector(root, bounds, _deadline_or_default(deadline, bounds, time.monotonic))
    for phase, arguments, worktree_new in _diff_arguments(
        comparison, base=base, target=target, branch=branch, root=root, deadline=collector.deadline
    ):
        collector.add_phase(phase, arguments, worktree_new)
    return collector.records


class _ChangeCollector:
    """Diff records of one request, each with its bounded old and new blob."""

    def __init__(self, root: Path, bounds: ImpactLimits, deadline: float) -> None:
        self.root = root
        self.bounds = bounds
        self.deadline = deadline
        self.records: list[dict] = []
        self.total_bytes = 0

    def add_phase(self, phase: str, arguments: list[str], worktree_new: bool) -> None:
        raw = _git(
            self.root,
            arguments,
            deadline=self.deadline,
            max_bytes=max(64 * 1024, self.bounds.max_files * 16 * 1024),
        )
        parsed = _parse_raw_records(raw, phase)
        if len(self.records) + len(parsed) > self.bounds.max_files:
            raise ValueError("changed file ceiling exceeded")
        for record in parsed:
            self._add_record(record, worktree_new)

    def _add_record(self, record: dict, worktree_new: bool) -> None:
        old_blob = self._object_blob(record["old_oid"])
        new_blob = self._new_blob(record, worktree_new)
        self.total_bytes += len(old_blob or b"") + len(new_blob or b"")
        if self.total_bytes > self.bounds.max_total_blob_bytes:
            raise ValueError("changed blob total ceiling exceeded")
        record["old_blob"] = old_blob
        record["new_blob"] = new_blob
        self.records.append(record)

    def _object_blob(self, oid: str) -> bytes | None:
        return _object_blob(self.root, oid, deadline=self.deadline, limit=self.bounds.max_blob_bytes)

    def _new_blob(self, record: dict, worktree_new: bool) -> bytes | None:
        if worktree_new and record["status"] != "D":
            return _worktree_blob(self.root, record["new_path"], self.bounds.max_blob_bytes)
        return self._object_blob(record["new_oid"])


_LEGACY_RANGE = r"([^.]\S*)\.\.([^.]\S*)"


def get_changed_files(git_range: str | None = None) -> list[str]:
    """Compatibility wrapper; ranges are accepted only as explicit commit pairs."""
    try:
        return _changed_paths(_changes_for_range(git_range))
    except (OSError, TimeoutError, ValueError):
        return []


def _changes_for_range(git_range: str | None) -> list[dict]:
    if git_range is None:
        return collect_git_changes(ROOT)
    match = re.fullmatch(_LEGACY_RANGE, git_range)
    if match is None:
        return []
    return collect_git_changes(
        ROOT, comparison="two-commits", base=match.group(1), target=match.group(2)
    )


def _changed_paths(changes: list[dict]) -> list[str]:
    return sorted({str(item["new_path"] or item["old_path"]) for item in changes})


def extract_symbols_from_file(file_path: Path) -> list[str]:
    """Extract names for the explicitly low-confidence textual fallback."""
    if not file_path.exists():
        return []
    try:
        return _parsed_symbols(file_path)
    except (ImportError, OSError, ValueError):
        return _textual_file_symbols(file_path)


def _parsed_symbols(file_path: Path) -> list[str]:
    from code_graph import parse_file

    parsed = parse_file(file_path)
    return sorted(
        {item["name"] for key in ("functions", "classes") for item in parsed.get(key, [])}
    )


def _textual_file_symbols(file_path: Path) -> list[str]:
    try:
        content = file_path.read_bytes()
    except OSError:
        return []
    return _textual_symbols(content)


def _textual_symbols(content: bytes | None) -> list[str]:
    if not content:
        return []
    text = content.decode("utf-8", errors="ignore")
    patterns = (r"\bdef\s+(\w+)", r"\bclass\s+(\w+)", r"\bfunction\s+(\w+)")
    return sorted({match.group(1) for pattern in patterns for match in re.finditer(pattern, text)})


def find_stale_wiki_pages(
    changed_symbols: list[str],
    *,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Legacy textual-name fallback; never represents graph evidence."""
    if not changed_symbols or not KNOWLEDGE_DIR.exists():
        return []
    bounds = _limits_or_default(limits)
    notes = _NoteWalk(bounds, deadline, cancelled).markdown_files()
    pages = _stale_pages(notes, changed_symbols, bounds, deadline, cancelled)
    return sorted(pages, key=_stale_page_order)


class _NoteWalk:
    """Bounded discovery of the Markdown notes under the notes root."""

    def __init__(
        self, bounds: ImpactLimits, deadline: float | None, cancelled: Callable[[], bool] | None
    ) -> None:
        self.bounds = bounds
        self.deadline = deadline
        self.cancelled = cancelled
        self.pending = [KNOWLEDGE_DIR]
        self.markdown: list[tuple[Path, int]] = []
        self.file_count = 0
        self.directory_count = 0
        self.total_bytes = 0

    def markdown_files(self) -> list[tuple[Path, int]]:
        while self.pending:
            _check_impact_stop(self.deadline, self.cancelled)
            self._scan(self.pending.pop())
        return self.markdown

    def _scan(self, current: Path) -> None:
        try:
            entries = os.scandir(current)
        except OSError:
            return
        with entries:
            for entry in entries:
                _check_impact_stop(self.deadline, self.cancelled)
                self._visit(entry)

    def _visit(self, entry: os.DirEntry) -> None:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            self._add_directory(entry)
            return
        if stat.S_ISREG(metadata.st_mode):
            self._add_file(entry, metadata)

    def _add_directory(self, entry: os.DirEntry) -> None:
        self.directory_count += 1
        if self.directory_count > self.bounds.max_note_dirs:
            raise ValueError("impact note directory ceiling exceeded")
        self.pending.append(Path(entry.path))

    def _add_file(self, entry: os.DirEntry, metadata: os.stat_result) -> None:
        self.file_count += 1
        if self.file_count > self.bounds.max_note_files:
            raise ValueError("impact note file ceiling exceeded")
        if entry.name.casefold().endswith(".md"):
            self._add_markdown(entry, metadata)

    def _add_markdown(self, entry: os.DirEntry, metadata: os.stat_result) -> None:
        if metadata.st_size > self.bounds.max_note_bytes:
            raise ValueError("impact note file byte ceiling exceeded")
        self.total_bytes += metadata.st_size
        if self.total_bytes > self.bounds.max_total_note_bytes:
            raise ValueError("impact note total byte ceiling exceeded")
        self.markdown.append((Path(entry.path), metadata.st_size))


def _stale_pages(
    notes: list[tuple[Path, int]],
    changed_symbols: list[str],
    bounds: ImpactLimits,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict]:
    results = []
    for markdown, expected_size in sorted(notes):
        _check_impact_stop(deadline, cancelled)
        page = _stale_page(markdown, expected_size, changed_symbols, bounds)
        if page is not None:
            results.append(page)
    return results


def _stale_page(
    markdown: Path, expected_size: int, changed_symbols: list[str], bounds: ImpactLimits
) -> dict | None:
    if markdown.name in SKIP_NAMES or "archive" in markdown.parts:
        return None
    content = _note_text(markdown, expected_size, bounds.max_note_bytes)
    if content is None or "status: superseded" in content:
        return None
    return _page_match(markdown, content, changed_symbols)


def _note_text(markdown: Path, expected_size: int, limit: int) -> str | None:
    raw = _captured_note(markdown, limit)
    if raw is None:
        return None
    if len(raw) != expected_size:
        raise PermissionError("impact note changed after discovery")
    return raw.decode("utf-8", errors="ignore")


def _captured_note(markdown: Path, limit: int) -> bytes | None:
    """The note's bytes; None when it vanished or cannot be read, never on a refusal."""
    try:
        return _capture_note_file(KNOWLEDGE_DIR, markdown, limit)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise
    except OSError:
        return None


def _page_match(markdown: Path, content: str, changed_symbols: list[str]) -> dict | None:
    matched = [
        symbol
        for symbol in changed_symbols
        if re.search(r"\b" + re.escape(symbol) + r"\b", content)
    ]
    if not matched:
        return None
    return {
        "slug": markdown.stem,
        "path": _vault_relative(markdown),
        "matched_symbols": matched,
        "confidence": _match_confidence(matched),
        "reason": f"mentions {len(matched)} changed symbol(s): {', '.join(matched[:5])}",
    }


def _match_confidence(matched: list[str]) -> str:
    if len(matched) >= 3:
        return "high"
    return "medium"


def _vault_relative(markdown: Path) -> str:
    try:
        return markdown.relative_to(ROOT).as_posix()
    except ValueError:
        return str(markdown)


def _stale_page_order(item: dict) -> tuple:
    return (item["confidence"] != "high", -len(item["matched_symbols"]), item["path"])


def apply_significance_budget(pages: list[dict], threshold: float = 0.8) -> list[dict]:
    """Retain the smallest prefix covering the requested textual-match weight."""
    if not pages or len(pages) <= 5:
        return pages
    total = sum(_match_weight(page) for page in pages)
    if total == 0:
        return pages
    return _weighted_prefix(pages, total, threshold)


def _match_weight(page: dict) -> int:
    return len(page.get("matched_symbols", []))


def _weighted_prefix(pages: list[dict], total: int, threshold: float) -> list[dict]:
    selected = []
    cumulative = 0
    for page in sorted(pages, key=_match_weight, reverse=True):
        selected.append(page)
        cumulative += _match_weight(page)
        if cumulative / total >= threshold:
            break
    return selected


def _changed_ranges(
    old: bytes | None, new: bytes | None, *, deadline: float | None = None
) -> list[dict]:
    _check_impact_stop(deadline)
    old_lines = (old or b"").splitlines(keepends=True)
    new_lines = (new or b"").splitlines(keepends=True)
    old_offsets = _line_offsets(old_lines, deadline)
    new_offsets = _line_offsets(new_lines, deadline)
    prefix = _common_prefix(old_lines, new_lines, deadline)
    if prefix == len(old_lines) == len(new_lines):
        return []
    suffix = _common_suffix(old_lines, new_lines, prefix, deadline)
    return [
        {
            "old": _range_side(prefix, len(old_lines) - suffix, old_offsets),
            "new": _range_side(prefix, len(new_lines) - suffix, new_offsets),
        }
    ]


def _line_offsets(lines: list[bytes], deadline: float | None) -> list[int]:
    offsets = [0]
    for line in lines:
        _check_impact_stop(deadline)
        offsets.append(offsets[-1] + len(line))
    return offsets


def _common_prefix(old_lines: list[bytes], new_lines: list[bytes], deadline: float | None) -> int:
    prefix = 0
    shared = min(len(old_lines), len(new_lines))
    while prefix < shared and old_lines[prefix] == new_lines[prefix]:
        _check_impact_stop(deadline)
        prefix += 1
    return prefix


def _common_suffix(
    old_lines: list[bytes], new_lines: list[bytes], prefix: int, deadline: float | None
) -> int:
    """Lines shared at the end, never reaching back into the shared prefix."""
    suffix = 0
    limit = min(len(old_lines), len(new_lines)) - prefix
    while suffix < limit and old_lines[-suffix - 1] == new_lines[-suffix - 1]:
        _check_impact_stop(deadline)
        suffix += 1
    return suffix


def _range_side(prefix: int, end: int, offsets: list[int]) -> dict:
    return {
        "line_start": prefix + 1,
        "line_end": max(prefix + 1, end),
        "byte_start": offsets[prefix],
        "byte_end": offsets[end],
    }


def _active_graph(root: Path, deadline: float):
    try:
        from evidence_graph import EvidenceGraph
        from generation_catalog import GenerationCatalog
        from repository_scope import resolve_repository_scope

        state_root = STATE_ROOT
        catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        if not catalog_path.is_file():
            return None
        scope = resolve_repository_scope(root, deadline=deadline)
        return EvidenceGraph.open_active_for_repository(
            GenerationCatalog(state_root, catalog_path=catalog_path),
            scope,
            deadline=deadline,
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, TypeError, ValueError):
        return None


def _overlaps(occurrence: dict, changed: dict) -> bool:
    byte_start = int(changed["byte_start"])
    byte_end = int(changed["byte_end"])
    if byte_end > byte_start:
        return int(occurrence["byte_start"]) < byte_end and int(occurrence["byte_end"]) > byte_start
    line = int(changed["line_start"])
    return int(occurrence["line_start"]) <= line <= int(occurrence["line_end"])


def _symbol_record(node: dict, occurrence: dict, side: str, changed: dict) -> dict:
    metadata = node.get("metadata", {})
    return {
        "node_id": node["node_id"],
        "name": metadata.get("name", node.get("identity_key", "")),
        "kind": node["kind"],
        "path": occurrence["relative_path"],
        "sides": [side],
        "classification": "exact",
        "evidence": {
            "path": occurrence["relative_path"],
            "line_start": occurrence["line_start"],
            "line_end": occurrence["line_end"],
            "changed_range": changed,
        },
    }


_SYMBOL_KINDS = frozenset({"symbol", "function", "method", "class"})


def _code_path(change: dict, side: str) -> str | None:
    """The path on `side` when it holds a code file the generation could index."""
    if change[f"{side}_blob"] is None:
        return None
    path = change[f"{side}_path"]
    if not path or Path(path).suffix.lower() not in CODE_EXTENSIONS:
        return None
    return path


def _changed_occurrence(graph, node: dict, path: str, old_range: dict, deadline: float):
    """The node's occurrence in `path` that the hunk's old range touches, or None."""
    for occurrence in graph.occurrences(node["node_id"], max_rows=32, deadline=deadline):
        if occurrence["relative_path"] == path and _overlaps(occurrence, old_range):
            return occurrence
    return None


def _note_symbol(symbols: dict, node: dict, occurrence: dict, side: str, old_range: dict) -> None:
    existing = symbols.get(node["node_id"])
    if existing is None:
        symbols[node["node_id"]] = _symbol_record(node, occurrence, side, old_range)
    elif side not in existing["sides"]:
        existing["sides"].append(side)


def _map_side(graph, symbols: dict, change: dict, changed_range: dict, side: str, bounds, deadline):
    """Name the symbols one side of one hunk touches.

    The overlap always uses the hunk's *old* range: the generation's
    occurrences are offsets into the bytes it indexed, and a grown line on
    the new side otherwise reached the next symbol (audit M13,
    docs/research/2026-09-11-a-grown-line-does-not-reach-the-next-symbol.md).
    """
    path = _code_path(change, side)
    if path is None:
        return
    old_range = changed_range["old"]
    for node in graph.find_nodes(path=path, max_rows=bounds.max_graph_rows, deadline=deadline):
        if node["kind"] not in _SYMBOL_KINDS:
            continue
        occurrence = _changed_occurrence(graph, node, path, old_range, deadline)
        if occurrence is not None:
            _note_symbol(symbols, node, occurrence, side, old_range)
            _require_symbol_ceiling(symbols, bounds)


def _require_symbol_ceiling(symbols: dict, bounds: ImpactLimits) -> None:
    if len(symbols) > bounds.max_symbols:
        raise ValueError("changed symbol ceiling exceeded")


def _range_sides(changes: list[dict]) -> list[tuple[dict, dict, str]]:
    """Every (change, hunk, side) the mapping visits, in diff order."""
    return [
        (change, changed_range, side)
        for change in changes
        for changed_range in change["ranges"]
        for side in ("old", "new")
    ]


def _map_symbols(graph, changes: list[dict], bounds: ImpactLimits, deadline: float) -> list[dict]:
    symbols: dict[str, dict] = {}
    for change, changed_range, side in _range_sides(changes):
        if time.monotonic() >= deadline:
            raise TimeoutError("impact analysis deadline reached")
        _map_side(graph, symbols, change, changed_range, side, bounds, deadline)
    return sorted(symbols.values(), key=lambda item: (item["path"], item["name"], item["node_id"]))


def _project_file_ids(graph, changes: list[dict], bounds: ImpactLimits, deadline: float) -> set[str]:
    """Resolve project-journal file values before following checkpoint edges."""
    paths = _changed_path_values(changes)
    if not paths:
        return set()
    nodes = graph.find_nodes(kinds=("file",), max_rows=bounds.max_graph_rows, deadline=deadline)
    return {str(node["node_id"]) for node in nodes if _file_node_value(node) in paths}


def _changed_path_values(changes: list[dict]) -> set[str]:
    return {
        str(change[key])
        for change in changes
        for key in ("old_path", "new_path")
        if change.get(key)
    }


def _file_node_value(node: dict) -> str:
    return str(node.get("metadata", {}).get("value", "")).replace("\\", "/")


def _edge_evidence(graph, assertion_id: str, bounds: ImpactLimits, deadline: float) -> list[dict]:
    try:
        rows = graph.evidence(assertion_id=assertion_id, max_rows=8, deadline=deadline)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return []
    return [
        {
            "path": row["relative_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "assertion_id": assertion_id,
        }
        for row in rows
    ]


def _empty_affected() -> dict[str, list[dict]]:
    return {"decisions": [], "pages": [], "tests": [], "checkpoints": []}


_KIND_GROUPS = {
    "decision": "decisions",
    "knowledge-page": "pages",
    "debugging-note": "pages",
    "checkpoint": "checkpoints",
}


def _affected_nodes(graph, symbol_ids: set[str], bounds: ImpactLimits, deadline: float) -> dict:
    edges = graph.edges(
        edge_types=tuple(sorted(TRAVERSED_EDGES)),
        max_rows=bounds.max_graph_rows,
        deadline=deadline,
    )
    used = _reaching_edges(edges, set(symbol_ids), bounds.max_depth)
    groups = _empty_affected()
    for node_id, edge in used.items():
        _add_affected(groups, graph, node_id, edge, bounds, deadline)
    for values in groups.values():
        values.sort(key=lambda item: (item["path"], item["name"], item["node_id"]))
    return groups


def _reaching_edges(edges: list[dict], reached: set[str], max_depth: int) -> dict[str, dict]:
    """The confirmed edge that first reached each source node, over at most max_depth rounds."""
    used: dict[str, dict] = {}
    for _depth in range(max_depth):
        if not _extend_reach(edges, reached, used):
            break
    return used


def _extend_reach(edges: list[dict], reached: set[str], used: dict[str, dict]) -> bool:
    added = False
    for edge in edges:
        if not _extends_reach(edge, reached):
            continue
        reached.add(edge["source_node_id"])
        used[edge["source_node_id"]] = edge
        added = True
    return added


def _extends_reach(edge: dict, reached: set[str]) -> bool:
    if edge.get("confidence") not in CONFIRMED_CONFIDENCE:
        return False
    return edge["target_node_id"] in reached and edge["source_node_id"] not in reached


def _add_affected(
    groups: dict, graph, node_id: str, edge: dict, bounds: ImpactLimits, deadline: float
) -> None:
    node = graph.node(node_id)
    if node is None:
        return
    metadata = node.get("metadata", {})
    path = str(metadata.get("path", node.get("identity_key", "")))
    group = _artifact_group(node["kind"], path, metadata)
    if group is None:
        return
    groups[group].append(
        {
            "node_id": node_id,
            "kind": node["kind"],
            "path": path,
            "name": metadata.get("name", node.get("identity_key", "")),
            "classification": "exact",
            "via": edge["edge_type"],
            "evidence": _edge_evidence(graph, edge["assertion_id"], bounds, deadline),
        }
    )


def _artifact_group(kind: str, path: str, metadata: dict) -> str | None:
    group = _KIND_GROUPS.get(kind)
    if group is not None:
        return group
    if _is_test_artifact(path, metadata):
        return "tests"
    return None


def _is_test_artifact(path: str, metadata: dict) -> bool:
    location = Path(path)
    return (
        location.name.startswith("test_")
        or "tests" in location.parts
        or str(metadata.get("name", "")).startswith("test_")
    )


def analyze_impact(
    git_range: str | None = None,
    *,
    root: Path = ROOT,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    graph=None,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Map explicit Git endpoints through canonical graph symbols and edges."""
    bounds = _limits_or_default(limits)
    comparison, base, target = _legacy_endpoints(git_range, comparison, base, target)
    root = Path(root).resolve(strict=True)
    deadline = _deadline_or_default(deadline, bounds, monotonic)
    _check_impact_stop(deadline, cancelled)
    run = _ImpactRun(bounds, deadline, cancelled)
    run.collect(root, comparison=comparison, base=base, target=target, branch=branch)
    run.describe()
    run.map_changes(graph, root)
    fallback = _textual_fallback(run.textual_names, bounds, deadline, cancelled)
    return run.report(comparison, fallback)


def _legacy_endpoints(
    git_range: str | None, comparison: str, base: str | None, target: str | None
) -> tuple[str, str | None, str | None]:
    if git_range is None:
        return comparison, base, target
    match = re.fullmatch(_LEGACY_RANGE, git_range)
    if match is None:
        raise ValueError("legacy git_range must contain exactly two commit endpoints")
    return "two-commits", match.group(1), match.group(2)


_PUBLIC_CHANGE_KEYS = (
    "comparison",
    "status",
    "similarity",
    "old_path",
    "new_path",
    "old_oid",
    "new_oid",
    "ranges",
)


class _ImpactRun:
    """One impact request: its changes, what the graph resolved, and what went wrong."""

    def __init__(
        self, bounds: ImpactLimits, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        self.bounds = bounds
        self.deadline = deadline
        self.cancelled = cancelled
        self.warnings: list[str] = []
        self.partial = False
        self.changes: list[dict] = []
        self.public_changes: list[dict] = []
        self.textual_names: set[str] = set()
        self.changed_symbols: list[dict] = []
        self.affected = _empty_affected()
        self.graph_missing = False
        self.generation_id = None

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.partial = True

    def collect(self, root: Path, **endpoints) -> None:
        try:
            self.changes = collect_git_changes(
                root, limits=self.bounds, deadline=self.deadline, **endpoints
            )
        except (InvalidRevisionError, TimeoutError):
            raise
        except ValueError as exc:
            self.warn(str(exc))

    def describe(self) -> None:
        for change in self.changes:
            _check_impact_stop(self.deadline, self.cancelled)
            change["ranges"] = _changed_ranges(
                change["old_blob"], change["new_blob"], deadline=self.deadline
            )
            self.textual_names.update(_textual_symbols(change["old_blob"]))
            self.textual_names.update(_textual_symbols(change["new_blob"]))
            self.public_changes.append({key: change[key] for key in _PUBLIC_CHANGE_KEYS})

    def map_changes(self, graph, root: Path) -> None:
        selected = _selected_graph(graph, root, self.deadline)
        if selected is None:
            self._note_missing_graph()
            return
        self.generation_id = getattr(selected, "generation_id", None)
        self._map_graph(selected, owns_graph=graph is None)

    def _note_missing_graph(self) -> None:
        self.graph_missing = True
        if self.changes:
            self.warnings.append("No valid active Evidence Graph generation is available.")

    def _map_graph(self, graph, *, owns_graph: bool) -> None:
        try:
            self._map_through(graph)
        except TimeoutError:
            raise
        except (OSError, ValueError) as exc:
            self.warn(str(exc))
        finally:
            if owns_graph:
                graph.close()

    def _map_through(self, graph) -> None:
        self.changed_symbols = _map_symbols(graph, self.changes, self.bounds, self.deadline)
        self.affected = _affected_nodes(graph, self._reached_ids(graph), self.bounds, self.deadline)
        if _has_unsourced_edge(self.affected):
            self.warn("One or more resolved impact edges have no source evidence path.")
        self.textual_names.update(str(item["name"]) for item in self.changed_symbols)
        if self.changes and not self.changed_symbols:
            self.warn("Changed ranges did not resolve to canonical symbols in the active graph.")

    def _reached_ids(self, graph) -> set[str]:
        symbol_ids = {item["node_id"] for item in self.changed_symbols}
        return symbol_ids | _project_file_ids(graph, self.changes, self.bounds, self.deadline)

    def classification(self) -> str:
        if self.graph_missing and self.changes:
            return "unresolved"
        if self.warnings or self.partial:
            return "conservative"
        return _resolution(self.changes, self.changed_symbols)

    def report(self, comparison: str, fallback: list[dict]) -> dict:
        return {
            "comparison": comparison,
            "generation_id": self.generation_id,
            "classification": self.classification(),
            "partial": self.partial,
            "warnings": self.warnings,
            "changes": self.public_changes,
            "changed_files": _changed_paths(self.public_changes),
            "changed_symbols": self.changed_symbols,
            "affected": self.affected,
            "textual_fallback": fallback,
            "stale_pages": fallback,
            "summary": self._summary(),
        }

    def _summary(self) -> str:
        affected = sum(len(values) for values in self.affected.values())
        return (
            f"{len(self.public_changes)} diff record(s), {len(self.changed_symbols)} canonical symbol(s), "
            f"{affected} affected artifact(s)."
        )


def _resolution(changes: list[dict], changed_symbols: list[dict]) -> str:
    """Exact when nothing changed or the changes resolved to symbols."""
    if not changes or changed_symbols:
        return "exact"
    return "unresolved"


def _selected_graph(graph, root: Path, deadline: float):
    if graph is not None:
        return graph
    return _active_graph(root, deadline)


def _has_unsourced_edge(affected: dict) -> bool:
    return any(not item["evidence"] for values in affected.values() for item in values)


def _textual_fallback(
    names: set[str], bounds: ImpactLimits, deadline: float, cancelled: Callable[[], bool] | None
) -> list[dict]:
    fallback = find_stale_wiki_pages(
        sorted(names),
        limits=bounds,
        deadline=deadline,
        cancelled=cancelled,
    )
    for item in fallback:
        item["confidence"] = "low"
        item["method"] = "textual-name-match"
        item["classification"] = "conservative"
    return apply_significance_budget(fallback)


def format_for_advisory(impact: dict, max_pages: int = 3) -> str:
    """Format the compatibility textual fallback for SessionStart."""
    stale = impact.get("stale_pages", [])
    if not stale:
        return ""
    lines = ["### Code-Knowledge Impact", impact["summary"], ""]
    for page in stale[:max_pages]:
        marker = "!!!" if page["confidence"] == "high" else "!"
        lines.append(f"{marker} **{page['slug']}** - {page['reason']}")
    if len(stale) > max_pages:
        lines.append(f"... and {len(stale) - max_pages} more.")
    return "\n".join(lines)


def main() -> int:
    import json

    parser = _argument_parser()
    arguments = parser.parse_args()
    try:
        impact = analyze_impact(
            comparison=arguments.comparison,
            base=arguments.base,
            target=arguments.target,
            branch=arguments.branch,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        parser.error(str(exc))
    if arguments.json:
        print(json.dumps(impact, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    _print_impact(impact)
    return 0


def _argument_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Diff-to-Evidence-Graph impact analysis")
    parser.add_argument("--comparison", choices=sorted(COMPARISONS), default="dirty")
    parser.add_argument("--base")
    parser.add_argument("--target")
    parser.add_argument("--branch")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_impact(impact: dict) -> None:
    print(impact["summary"])
    print(f"Classification: {impact['classification']}")
    for group, values in impact["affected"].items():
        if values:
            print(f"{group}: {', '.join(str(item['name']) for item in values)}")
    if impact["warnings"]:
        print("Warnings: " + "; ".join(impact["warnings"]))


if __name__ == "__main__":
    raise SystemExit(main())
