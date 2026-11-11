"""Compile receipts, daily parts and the native frames a receipt names."""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

from bounded_io import (
    VerifiedByteRange,
    immutable_source_digest,
    open_stable_range,
    read_stable_bytes,
    require_immutable_source,
)
from evidence_resolver import (
    MAX_DAILY_BYTES,
    daily_entries,
    daily_part_bounds,
    source_range_digest,
)
from llm_wiki import runtime_roots
from llm_wiki.compile_receipt_authority import (
    MAX_RECEIPT_BYTES,
    read_compile_receipt_version,
    require_transaction_authority,
)
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.receipts import COMPILE_RECEIPT_V4_SCHEMA as COMPILE_RECEIPT_V4_SCHEMA
from llm_wiki.formats.receipts import (
    compile_context_source_identity as compile_context_source_identity,
)
from llm_wiki.formats.receipts import compile_source_identity as compile_source_identity
from llm_wiki.formats.receipts import corrupt_receipt as corrupt_receipt
from llm_wiki.formats.receipts import parse_compile_receipt_v3 as parse_compile_receipt_v3
from llm_wiki.formats.receipts import parse_compile_receipt_v4 as parse_compile_receipt_v4
from llm_wiki.formats.receipts import receipt_source_fields as _receipt_source_fields
from llm_wiki.formats.receipts import require_v4_descriptor as require_v4_descriptor
from markdown_transaction import MarkdownCoordinator
from settings import setting_value


@dataclass(frozen=True)
class NativeCompileFrame:
    source_path: str
    byte_start: int
    byte_end: int
    timestamp: str
    encoded: str
    text: str


@dataclass(frozen=True, eq=False)
class NativeFrameViews(Sequence):
    content: bytes | VerifiedByteRange
    coordinates: tuple

    def __len__(self):
        require_immutable_source(self.content)
        return len(self.coordinates)

    def __getitem__(self, index):
        require_immutable_source(self.content)
        if isinstance(index, slice):
            return NativeFrameViews(self.content, self.coordinates[index])
        return _native_frame_from_coordinates(self.content, self.coordinates[index])

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(left == right for left, right in zip(self, other))


def _native_frame_from_coordinates(content, coordinates):
    from event_envelope import native_user_text

    path, start, end, timestamp = coordinates
    encoded = content[start:end].decode()
    text = native_user_text("    " + encoded)
    if text is None:
        raise ValueError("native frame source no longer names a user event")
    return NativeCompileFrame(path, start, end, timestamp, encoded, text)


def native_frame_spans(frames):

    if type(frames) is NativeFrameViews:
        require_immutable_source(frames.content)
        return ((row[1], row[2]) for row in frames.coordinates)
    if type(frames) is RestoredNativeFrames:
        return frames.spans()
    return ((frame.byte_start, frame.byte_end) for frame in frames)


@dataclass(frozen=True)
class DailySnapshot:
    logical_path: str
    content: bytes
    sha256: str
    # Where this snapshot sits inside the day it came from. A day that fits the
    # compile budget is one part covering the whole file; a longer one is split
    # at entry boundaries, and every part still names the byte range it is, so
    # anything compiled from it points back at a real span of a real file.
    part_index: int = 0
    part_count: int = 1
    byte_start: int = 0
    byte_end: int = 0
    original_content: bytes | VerifiedByteRange | None = None
    original_sha256: str = ""
    original_entries: tuple[tuple[str, int, int], ...] = ()
    native_frames: Sequence[NativeCompileFrame] = ()
    already_compiled: bool = False

    @property
    def part_key(self) -> str:
        """What identifies this part while batching; the path when there is one."""
        if self.part_count == 1:
            return self.logical_path
        return f"{self.logical_path}@{self.byte_start}-{self.byte_end}"


@dataclass(frozen=True)
class SourceSnapshot:
    logical_path: str
    content: bytes
    sha256: str
    prompt_content: bytes | None = None


def daily_read_budget():
    quota = setting_value("compile.max_total_source_bytes", runtime_roots.vault_root())
    if quota == 0:
        return MAX_DAILY_BYTES
    return min(MAX_DAILY_BYTES, quota)


def open_daily_source(path, *, deadline=None):
    deadline = None if deadline == math.inf else deadline
    return open_stable_range(path, daily_read_budget(), label="daily source", deadline=deadline)


def compile_receipt_path(source_identity: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", source_identity) is None:
        raise ValueError("source identity must be lowercase 64-hex")
    return runtime_roots.daily_dir() / "receipts" / f"v3-{source_identity}.md"


def iter_daily_parts(logical_path, content, compiled=None, *, native_frames=(), bounds=None):
    """Construct part bodies only as the consumer requests their atomic units."""
    bounds = daily_part_bounds(content) if bounds is None else bounds
    original_digest = immutable_source_digest(content)
    original_entries = tuple(daily_entries(content))
    parts = (
        daily_part_record(logical_path, content, bounds, index,
                           original_digest, original_entries, native_frames)
        for index in range(len(bounds))
    )
    yield from iter_pending_with_native_context(parts, compiled)


def daily_part_record(logical, original, bounds, index, digest, entries, frames):
    start, end = bounds[index]
    payload = original[start:end]
    return DailySnapshot(
        logical, payload, sha256_bytes(payload),
        part_index=index, part_count=len(bounds), byte_start=start, byte_end=end,
        original_content=original, original_sha256=digest,
        original_entries=entries, native_frames=frames,
    )


def iter_pending_with_native_context(parts, compiled):
    if compiled is None:
        yield from parts
        return
    marked = (replace(part, already_compiled=snapshot_compiled(compiled, part)) for part in parts)
    yield from _pending_native_unit_parts(iter_native_part_units(marked))


def _pending_native_unit_parts(units):
    for unit in units:
        if _native_unit_pending(unit):
            yield from unit


def _native_unit_pending(unit):
    return not all(part.already_compiled for part in unit)


def iter_native_part_units(parts, *, join=None):
    """Keep the current atomic unit and the first part of the next one."""
    current = []
    predicate = join or _native_parts_join
    for part in parts:
        if _native_unit_boundary(current, part, predicate):
            yield current
            current = []
        current.append(part)
    if current:
        yield current


def _native_unit_boundary(current, part, predicate):
    return bool(current) and not predicate(current[-1], part)


def _native_parts_join(left, right):
    if left.logical_path != right.logical_path or left.byte_end != right.byte_start:
        return False
    return (any(start < left.byte_end < end for start, end in native_frame_spans(left.native_frames))
            or captured_tool_cut(left, left.byte_end))


def captured_tool_cut(part, offset):
    """A pure immutable-source boundary test, never a native-user authority."""
    original = part.original_content
    if type(original) not in (bytes, VerifiedByteRange) or not 0 < offset < len(original):
        return False
    if original[offset - 1:offset] == b"\n":
        return False
    return is_physical_tool_line(_line_at_source_offset(original, offset))


def _line_at_source_offset(original, offset):
    start = original.rfind(b"\n", 0, offset) + 1
    end = original.find(b"\n", offset)
    if end < 0:
        end = len(original)
    return original[start:end]


def is_physical_tool_line(line):
    if not line.startswith(b"    {"):
        return False
    try:
        record = json.loads(line[4:])
        return (isinstance(record, dict) and record.get("event_type") == "post_tool_use"
                and canonical_json_bytes(record) == line[4:])
    except (TypeError, ValueError, UnicodeDecodeError):
        return False


def snapshot_compiled(compiled: Callable[[str, str], bool], part: DailySnapshot) -> bool:
    if isinstance(compiled, ContextReceiptSelector):
        return compiled.matches(part)
    return compiled(part.logical_path, part.sha256)


def physical_source(part: DailySnapshot) -> SourceSnapshot:
    content = part.original_content
    if content is None:
        return SourceSnapshot(part.logical_path, part.content, part.sha256)
    return SourceSnapshot(part.logical_path, content, part.original_sha256)


def snapshot_absolute_bounds(part: DailySnapshot) -> tuple[int, int]:
    if part.original_content is not None:
        return part.byte_start, part.byte_end
    return _standalone_snapshot_bounds(part)


def _standalone_snapshot_bounds(part):
    if part.part_count != 1 or part.byte_start != 0 or part.byte_end not in {0, len(part.content)}:
        raise ValueError("compile source original context is absent")
    return 0, len(part.content)


def _receipt_reader_active(deadline: float | None) -> None:
    if deadline is not None:
        require_compile_active(deadline, None)


def read_compile_receipt_v3(
    logical_path: str,
    source_sha256: str,
    coordinator: MarkdownCoordinator,
    *,
    path: Path | None = None,
    vault: Path | None = None,
    deadline: float | None = None,
) -> dict[str, object] | None:
    _receipt_reader_active(deadline)
    source_identity = compile_source_identity(logical_path, source_sha256)
    path = compile_receipt_path(source_identity) if path is None else Path(path)
    vault = runtime_roots.vault_root() if vault is None else Path(vault)
    try:
        raw_bytes = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compile receipt")
    except FileNotFoundError:
        return None
    try:
        _require_receipt_name(path, source_identity)
        record = parse_compile_receipt_v3(
            raw_bytes,
            logical_path=logical_path,
            source_sha256=source_sha256,
        )
        require_transaction_authority(record, coordinator, path, vault, raw_bytes, deadline=deadline)
        _receipt_reader_active(deadline)
        return record
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise corrupt_receipt(exc, path) from exc


def _require_receipt_name(path: Path, source_identity: str) -> None:
    if path.name != f"v3-{source_identity}.md":
        raise ValueError("compile receipt path identity disagrees")


RECEIPT_DISCOVERY_PARSING = ContextVar("receipt_discovery_parsing", default=None)


def validated_discovery_source(raw, fields):
    record = parse_compile_receipt_v4(raw, logical_path=fields[0], source_sha256=fields[1])
    return record["source"]


def _discovered_receipt_source(path, raw, fields):
    parsing = RECEIPT_DISCOVERY_PARSING.get()
    if parsing is None:
        return validated_discovery_source(raw, fields)
    return parsing.source(path, raw, fields)


class ContextReceiptSelector:
    """One deadline-aware discovery pass; every positive result verifies authority."""

    def __init__(self, coordinator: MarkdownCoordinator, deadline: float) -> None:
        self.coordinator = coordinator
        self.vault = coordinator.vault
        self.deadline = deadline
        self.catalog: dict[tuple[str, str], list[tuple[Path, dict[str, object]]]] | None = None
        self.prefix_hashes: dict[tuple[str, int], str] = {}
        self.prefix_source = None
        self.historical_receipts: dict[tuple[str, str], tuple | None] = {}

    def _active(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("compile receipt discovery deadline exceeded")

    def _load(self) -> None:
        self._active()
        if self.catalog is not None:
            return
        catalog = {}
        for path in v4_receipt_paths(self.vault / "knowledge/daily/receipts", self._active):
            self._index_receipt(catalog, path)
        self._active()
        self.catalog = catalog

    def _index_receipt(self, catalog, path):
        raw = read_stable_bytes(path, MAX_RECEIPT_BYTES, label="compile receipt")
        fields = _receipt_source_fields(raw)
        if fields is None:
            raise ValueError("compile receipt source identity is absent")
        source = _discovered_receipt_source(path, raw, fields)
        if path.name != f"v4-{compile_context_source_identity(source)}.md":
            raise ValueError("compile receipt filename disagrees")
        catalog.setdefault(fields, []).append((path, source))
        self._active()

    def __call__(self, logical_path: str, digest: str) -> bool:
        self._active()
        with self._source_parts(logical_path) as parts:
            return self._matching_parts_are_compiled(parts, digest)

    @contextmanager
    def _source_parts(self, logical_path):
        self._active()
        self.prefix_source = None
        try:
            with open_daily_source(self.vault / logical_path, deadline=self.deadline) as content:
                yield iter_daily_parts(logical_path, content)
                self._active()
        finally:
            self.prefix_source = None

    def matches_saved_source(self, source):
        self._active()
        _require_saved_context_source(source)
        with self._source_parts(source["logical_path"]) as parts:
            return any(self._saved_source_matches_part(source, part) for part in parts)

    def _saved_source_matches_part(self, source, part):
        if part.sha256 != source["sha256"] or not self._context_matches(source, part):
            return False
        return self.matches(part)

    def _matching_parts_are_compiled(self, parts, digest):
        return all_present_parts(parts_with_digest(parts, digest), self.matches)

    def matches(self, part: DailySnapshot) -> bool:
        return self.receipt(part) is not None

    def receipt(self, part: DailySnapshot):
        self._validate_historical_part(part)
        self._load()
        candidates = self.catalog.get((part.logical_path, part.sha256), ())
        for path, source in candidates:
            record = self._authoritative_match(path, source, part)
            if record is not None:
                return record
        return None

    def _validate_historical_part(self, part):
        self._active()
        key = (part.logical_path, part.sha256)
        identity = compile_source_identity(*key)
        path = self.vault / "knowledge/daily/receipts" / f"v3-{identity}.md"
        file_identity = _historical_receipt_file_identity(path)
        if key in self.historical_receipts and self.historical_receipts[key] == file_identity:
            return
        read_compile_receipt_v3(*key, self.coordinator, path=path, vault=self.vault, deadline=self.deadline)
        self._active()
        self.historical_receipts[key] = file_identity


    def _authoritative_match(self, path, source, part):
        self._active()
        if not self._context_matches(source, part):
            return None
        record = read_compile_receipt_version(
            part.logical_path, part.sha256, self.coordinator, path=path, vault=self.vault, deadline=self.deadline
        )
        self._active()
        return record

    def _context_matches(self, source, part):
        physical = physical_source(part)
        require_immutable_source(physical.content)
        size = source["original_byte_size"]
        if not self._context_source_matches(source, part, physical):
            return False
        key = (physical.sha256, size)
        if key not in self.prefix_hashes:
            self.prefix_hashes[key] = source_range_digest(physical.content, 0, size)
        return self.prefix_hashes[key] == source["original_sha256"]


    def _context_source_matches(self, source, part, physical):
        within = source['original_byte_size'] <= len(physical.content)
        same_bounds = (source['byte_start'], source['byte_end']) == snapshot_absolute_bounds(part)
        return within and same_bounds and self._physical_digest(physical.content) == physical.sha256

    def _physical_digest(self, content):
        if self.prefix_source is None or self.prefix_source[0] is not content:
            self.prefix_source = content, immutable_source_digest(content)
        return self.prefix_source[1]


def _historical_receipt_file_identity(path):
    try:
        return _daily_file_identity(path)
    except FileNotFoundError:
        return None


def _daily_file_identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _require_saved_context_source(source):
    from llm_wiki.formats.schema import validate_schema_object

    document = json.loads(COMPILE_RECEIPT_V4_SCHEMA.read_text(encoding='utf-8'))
    schema = {"$defs": document["$defs"], **document["$defs"]["source_descriptor"]}
    validate_schema_object(source, schema)
    require_v4_descriptor(source)
    compile_source_identity(source["logical_path"], source["sha256"])


def v4_receipt_paths(directory: Path, active):
    active()
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return
    with entries:
        yield from _v4_receipt_entry_paths(entries, active)


def _v4_receipt_entry_paths(entries, active):
    for entry in entries:
        active()
        if entry.name.startswith("v4-") and entry.name.endswith(".md"):
            yield Path(entry.path)


def receipt_predicate(coordinator: MarkdownCoordinator, *, deadline: float = math.inf):
    return ContextReceiptSelector(coordinator, deadline)


def require_compile_active(
    deadline: float, cancelled: Callable[[], bool] | None
) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("compile deadline or cancellation reached")


def parts_with_digest(parts, digest):
    return (part for part in parts if part.sha256 == digest)


def all_present_parts(parts, predicate):
    found = False
    for part in parts:
        found = True
        if not predicate(part):
            return False
    return found


def native_frame_from_json(value):
    from compile_receipts import (
        NativeCompileFrame,
    )

    return NativeCompileFrame(**value)


class RestoredNativeFrames(Sequence):
    def __init__(self, values):
        self.values = values

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[position] for position in range(len(self))[index])
        return native_frame_from_json(self.values[index])

    def __eq__(self, other):
        return archive_sequences_equal(self, other)

    def spans(self):
        self.values.source.require_active()
        return iter(self.values.native_spans)


def archive_sequences_equal(left, right):
    if not isinstance(right, Sequence):
        return NotImplemented
    return len(left) == len(right) and all(a == b for a, b in zip(left, right))


def daily_is_compiled(
    logical_path: str, content: bytes, compiled: Callable[[str, str], bool]
) -> bool:
    """Whether every part of this day already has a receipt."""
    return all(snapshot_compiled(compiled, part) for part in iter_daily_parts(logical_path, content))
