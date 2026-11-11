"""The compile's daily sources: snapshots, native frames and captured partitions."""

from __future__ import annotations

import json
import re
import sys
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING

from bounded_io import (  # noqa: E402
    MAX_KNOWLEDGE_PAGE_BYTES,
    VerifiedByteRange,
    immutable_source_digest,
    read_stable_bytes,
    require_immutable_source,
    same_immutable_source,
)
from compile_cache import (  # noqa: E402
    CompileActionDescriptor,
)
from compile_receipts import (
    DailySnapshot,
    NativeCompileFrame,
    NativeFrameViews,
    SourceSnapshot,
    captured_tool_cut,
    daily_read_budget,
    iter_daily_parts,
    iter_native_part_units,
    iter_pending_with_native_context,
    native_frame_spans,
    open_daily_source,
    physical_source,
)
from evidence_resolver import (  # noqa: E402
    MAX_DAILY_PART_BYTES,  # noqa: F401 - re-exported: callers read the writer's bound here
    daily_part_bounds,
)
from llm_wiki.formats.canonical import sha256_bytes  # noqa: E402
from llm_wiki.formats.source import SourceDescriptor, SourceOccurrenceBounds  # noqa: E402
from memory_state import daily_logs
from settings import raise_hint, setting_value  # noqa: E402
from vault_log import LOG_NAME  # noqa: E402

if TYPE_CHECKING:
    pass
from llm_wiki import runtime_roots
from llm_wiki.compile_pipeline_base import (
    MAX_SOURCE_BYTES,
    SOURCE_CHOICE_PARSING,
)


def _native_frame_views(content, observed):
    coordinates = tuple((frame.source_path, frame.byte_start, frame.byte_end, frame.timestamp)
                        for frame in observed if frame is not None)
    if not coordinates:
        return ()
    return NativeFrameViews(content, coordinates)


@dataclass(frozen=True)
class TargetSnapshot:
    logical_path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class CompileInputs:
    dailies: tuple[DailySnapshot, ...]
    sources: Sequence[SourceSnapshot]
    targets: Sequence[TargetSnapshot]
    # The complete original vault context, even when `sources` is narrowed for
    # a prompt. Publication rereads derived index/log targets under its writer
    # gate; this model-input snapshot remains immutable.
    vault_files: Sequence[SourceSnapshot] = ()
    partition_context: object | None = dataclass_field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class CompilePackingIdentity:
    algorithm: str
    tokenizer_identity: str
    count_source: str
    max_input_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    measured_input_tokens: int

    def canonical(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "tokenizer_identity": self.tokenizer_identity,
            "count_source": self.count_source,
            "max_input_tokens": self.max_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "measured_input_tokens": self.measured_input_tokens,
        }


@dataclass(frozen=True)
class CompileBatch:
    inputs: CompileInputs
    manifest: tuple[SourceDescriptor, ...]
    manifest_sha256: str
    packing: CompilePackingIdentity
    planning_model: str | None = dataclass_field(default=None, compare=False)
    planning_candidates: tuple | None = dataclass_field(default=None, compare=False, repr=False)
    context_pending: bool = dataclass_field(default=False, compare=False)
    required_context_paths: tuple[str, ...] = dataclass_field(default=(), compare=False)


@dataclass(frozen=True)
class ResolvedCompilePlan:
    plan: dict[str, object]
    action: CompileActionDescriptor
    action_key: str
    cache_hit: bool
    provider_budget: Mapping[str, object]


@dataclass(frozen=True)
class CompileApplyResult:
    transaction_id: str | None
    operation_id: str
    state: str
    touched: tuple[str, ...]
    commit_sequence: int
    committed_at: str
    action_key: str


def logical_path(path: Path) -> str:
    return path.relative_to(runtime_roots.vault_root()).as_posix()


def _read_daily_source(path: Path) -> bytes:
    """Use the existing daily evidence contract within the total source budget."""
    return read_stable_bytes(path, daily_read_budget(), label="daily source")


@contextmanager
def open_optional_daily_source(path, *, deadline=None):
    with ExitStack() as owner:
        yield _enter_optional_daily_source(owner, path, deadline)


def _enter_optional_daily_source(owner, path, deadline):
    try:
        return owner.enter_context(open_daily_source(path, deadline=deadline))
    except FileNotFoundError:
        return None


def compile_source_snapshot(path: Path, *, label: str = "compile source", maximum: int | None = None) -> SourceSnapshot:
    if maximum is None:
        maximum = MAX_SOURCE_BYTES
    content = read_stable_bytes(path, maximum, label=label)
    return SourceSnapshot(logical_path(path), content, sha256_bytes(content))


def snapshot_compile_inputs(
    paths: Sequence[Path],
    *,
    compiled: Callable[[str, str], bool] | None = None,
) -> CompileInputs:
    """Capture every compile input once, before any model call.

    `compiled` answers whether one part of a day already has its receipt. A run
    interrupted partway leaves receipts for the parts that committed, and those
    parts are not offered again.
    """
    groups = {'dailies': [], 'sources': [], 'targets': [], 'vault_files': []}
    partitions = CapturedPartitions()
    for group, record in iter_compile_records(paths, compiled=compiled, partitions=partitions):
        groups[group].append(record)
    groups['sources'].sort(key=lambda record: record.logical_path)
    groups['targets'].sort(key=lambda record: record.logical_path)
    return partitions.bind(CompileInputs(**{name: tuple(records) for name, records in groups.items()},
                                          partition_context=partitions))


def iter_compile_records(paths, *, compiled=None, partitions=None):
    """Emit captured records without retaining completed source bodies.

    A supplied partition context belongs to the eager caller. Otherwise each
    daily's proof cache expires after its records have been consumed.
    """
    budget = SourceBudget()
    for path in sorted(map(Path, paths), key=lambda item: item.as_posix()):
        yield from _daily_capture_records(path, compiled, partitions, budget)
    for source in _vault_file_snapshots(budget.add):
        yield 'sources', source
        yield 'vault_files', source
    for target in _knowledge_targets(budget.add):
        yield 'sources', SourceSnapshot(target.logical_path, target.content, target.sha256)
        yield 'targets', target


def _daily_capture_records(path, compiled, partitions, budget):
    if partitions is not None:
        yield from daily_source_records(path, _read_daily_source(path), compiled, partitions, budget)
        return
    with open_daily_source(path) as content:
        yield from daily_source_records(path, content, compiled, partitions, budget)


def daily_source_records(path, content, compiled, partitions, budget):
    context = partitions or CapturedPartitions()
    logical = logical_path(path)
    parts = (context.iter_daily(logical, content, compiled) if partitions is None
             else context.daily(logical, content, compiled))
    for part in parts:
        yield 'dailies', part
    source = SourceSnapshot(logical, content, immutable_source_digest(content))
    budget.add(source)
    yield 'sources', source


def _vault_file_snapshots(
    add_source: Callable[[SourceSnapshot], None],
) -> Iterable[SourceSnapshot]:
    """Yield each complete vault file without retaining previous bodies."""
    for path in _vault_source_paths():
        if not path.exists():
            continue
        snapshot = compile_source_snapshot(path)
        add_source(snapshot)
        yield snapshot


class SourceBudget:
    """Enforce explicit aggregate quotas; zero leaves that quota disabled."""

    def __init__(self) -> None:
        self._source_count = 0
        self._total_bytes = 0
        self._max_sources = setting_value("compile.max_sources", runtime_roots.vault_root())
        self._max_total_bytes = setting_value("compile.max_total_source_bytes", runtime_roots.vault_root())

    def add(self, source: SourceSnapshot) -> None:
        if self._max_sources and self._source_count >= self._max_sources:
            raise ValueError(f"compile source count exceeds limit; {raise_hint('compile.max_sources')}")
        self._total_bytes += len(source.content)
        if self._max_total_bytes and self._total_bytes > self._max_total_bytes:
            raise ValueError(f"compile source bytes exceed limit; {raise_hint('compile.max_total_source_bytes')}")
        self._source_count += 1


def _knowledge_targets(
    add_source: Callable[[SourceSnapshot], None],
) -> Iterable[TargetSnapshot]:
    """Yield each live knowledge page's target after source admission."""
    for path in _live_knowledge_pages():
        source = compile_source_snapshot(path, label="knowledge page", maximum=MAX_KNOWLEDGE_PAGE_BYTES)
        add_source(source)
        yield TargetSnapshot(source.logical_path, source.content, source.sha256)


def _live_note_paths(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [path for path in sorted(directory.rglob("*.md")) if "archive" not in path.parts]


def _live_knowledge_pages() -> list[Path]:
    return _live_note_paths(runtime_roots.notes_dir())


def _vault_source_paths(root: Path | None = None) -> tuple[Path, Path, Path]:
    root = root or runtime_roots.vault_root()
    candidates = (root / "docs" / "AGENTS.md", root / "AGENTS.md")
    agents = next((path for path in candidates if path.exists()), candidates[0])
    return agents, root / "knowledge" / "index.md", root / "knowledge" / LOG_NAME


def compile_scope_paths(root: Path) -> list[Path]:
    """Forecast all eligible daily inputs using the compiler's source selectors."""
    metadata = [path for path in _vault_source_paths(root) if path.exists()]
    return _live_note_paths(root / "knowledge" / "notes") + daily_logs(root / "knowledge" / "daily") + metadata


def daily_parts(
    logical_path: str,
    content: bytes,
    compiled: Callable[[str, str], bool] | None = None,
    *, native_frames: Sequence[NativeCompileFrame] = (),
    bounds=None,
) -> list[DailySnapshot]:
    """This day as the one or more parts the compiler still has to take."""
    return list(iter_daily_parts(logical_path, content, compiled,
                                 native_frames=native_frames, bounds=bounds))


def native_daily_frames(logical_path, content, vault):
    from fact_keys import native_journal_index

    observed = (verified_native_frame(logical_path, content, start, head, block, vault)
                for start, (head, block) in native_journal_index(content).items())
    return _native_frame_views(content, observed)


def verified_native_frame(path, content, start, head, block, vault):
    from breadcrumb_decision import journal_block
    from breadcrumb_evidence import read_head, read_permanent_source, read_source_document
    from event_envelope import native_user_text

    physical = block.rsplit(b"\n", 2)[-2]
    text = native_user_text(physical.decode())
    if text is None:
        return None
    encoded = physical[4:]
    _, anchor = read_head(read_source_document(vault, head))
    expected = journal_block(json.loads(anchor), head, read_permanent_source(vault, head)).encode()
    if expected != block or content[start:start + len(physical)] != physical:
        raise ValueError("native compile frame lacks canonical physical capture proof")
    timestamp = re.search(rb"## \[([0-9:]{8})\] Captured event", block)[1].decode()
    return NativeCompileFrame(path, start + 4, start + len(physical), timestamp, encoded.decode(), text)


def pending_with_native_context(parts, compiled):
    return list(iter_pending_with_native_context(parts, compiled))


def pending_daily_parts(inputs):
    return tuple(part for part in inputs.dailies if not part.already_compiled)


def native_part_units(parts, *, join=None):
    return list(iter_native_part_units(parts, join=join))


def require_tool_line_cover(parts):
    boundaries = ((parts[0], parts[0].byte_start), (parts[-1], parts[-1].byte_end))
    if any(captured_tool_cut(part, offset) for part, offset in boundaries):
        raise ValueError("captured tool line requires all covering source parts")


def tool_parts_join(left, right):
    if left.logical_path != right.logical_path or left.byte_end != right.byte_start:
        return False
    return captured_tool_cut(left, left.byte_end)


def source_descriptor(snapshot: DailySnapshot) -> SourceDescriptor:
    return SourceDescriptor(
        snapshot.logical_path,
        len(snapshot.content),
        snapshot.sha256,
        _daily_occurrence_bounds(snapshot.content),
    )


def _daily_occurrence_bounds(content: bytes) -> SourceOccurrenceBounds | None:
    event_ids = re.findall(rb"(?m)^event_id:\s*([!-~]{1,256})\s*$", content)
    if not event_ids:
        return None
    decoded = [value.decode("ascii", errors="strict") for value in event_ids]
    return SourceOccurrenceBounds(decoded[0], decoded[-1])


def subset_compile_inputs(
    inputs: CompileInputs,
    daily_paths: set[str],
    optional_paths: set[str] | None = None,
    *, journal_indexes=None, partitions=None,
) -> CompileInputs:
    all_daily_paths = {item.logical_path for item in inputs.dailies}
    selected = tuple(item for item in inputs.dailies if item.part_key in daily_paths)
    context = context_sources(inputs, all_daily_paths, optional_paths)
    selected_sources = deduplicated_sources(selected, journal_indexes=journal_indexes, partitions=partitions)
    return CompileInputs(
        selected,
        tuple(
            sorted((*selected_sources, *context), key=lambda item: item.logical_path)
        ),
        inputs.targets,
        inputs.vault_files,
    )


def context_sources(
    inputs: CompileInputs, daily_paths: set[str], optional_paths: set[str] | None
) -> tuple[SourceSnapshot, ...]:
    """The non-daily pages this batch was given room to carry."""
    return tuple(select_snapshot_paths(inputs.sources, (optional_paths or set()) - daily_paths))


def snapshot_paths(records):
    paths = getattr(records, 'logical_paths', None)
    if paths is not None:
        return paths()
    return tuple(record.logical_path for record in records)


def select_snapshot_paths(records, wanted):
    select = getattr(records, 'select_paths', None)
    if select is not None:
        return select(wanted)
    return tuple(record for record in records if record.logical_path in wanted)


def deduplicated_sources(
    selected: Sequence[DailySnapshot],
    *, journal_indexes=None, partitions=None,
) -> list[SourceSnapshot]:
    """One source per day preserves every byte of verified contiguous parts."""
    grouped = {}
    for part in selected:
        grouped.setdefault(part.logical_path, []).append(part)
    return [native_unit_source(parts, journal_indexes=journal_indexes, partitions=partitions) for parts in grouped.values()]


def native_unit_source(parts, *, journal_indexes=None, partitions=None):
    ordered = sorted(parts, key=lambda item: item.byte_start)
    require_native_unit(ordered, partitions=partitions)
    require_tool_line_cover(ordered)
    raw = b"".join(part.content for part in ordered)
    projected = _native_prompt_content(ordered, raw, journal_indexes=journal_indexes)
    return SourceSnapshot(ordered[0].logical_path, raw, sha256_bytes(raw), projected)


def require_native_unit(parts, *, partitions=None):
    if len(parts) == 1:
        return
    _require_contiguous_daily_parts(parts, partitions=partitions)


def partition_for_join(first, *, journal_indexes=None):
    original = first.original_content
    require_immutable_source(original)
    if immutable_source_digest(original) != first.original_sha256:
        raise ValueError("canonical original source digest does not match")
    return daily_part_bounds(original), _native_partition_ranges(first, journal_indexes)


def _native_partition_ranges(part, journal_indexes):
    spans = []
    for start, (_, block) in journal_index_for_part(part, journal_indexes).items():
        span = _native_partition_span(start, block)
        if span is not None:
            spans.append(span)
    return tuple(spans)


def _native_partition_span(start, block):
    from event_envelope import native_user_text

    physical = block.rsplit(b"\n", 2)[-2]
    if native_user_text(physical.decode()) is None:
        return None
    return start + 4, start + len(physical)


def _require_native_partition_cover(parts, spans):
    first, last = parts[0].byte_start, parts[-1].byte_end
    overlapping = _overlapping_partition_spans(spans, first, last)
    if any(start < first or end > last for start, end in overlapping):
        raise ValueError("native frame requires all covering source parts")


def _overlapping_partition_spans(spans, first, last):
    return [span for span in spans if span[0] < last and span[1] > first]


def _require_join_identity(part, first):
    if part.logical_path != first.logical_path or part.original_sha256 != first.original_sha256:
        raise ValueError("joined daily parts name different physical sources")
    if not same_immutable_source(part.original_content, first.original_content):
        raise ValueError("joined daily parts contain different original bytes")


def _require_join_ordinal(part, bounds):
    if type(part.part_index) is not int or type(part.part_count) is not int:
        raise ValueError("joined daily partition ordinals must be integers")
    if part.part_count != len(bounds) or not 0 <= part.part_index < len(bounds):
        raise ValueError("joined daily partition ordinal differs from its original")


def require_join_member(part, bounds):
    _require_join_ordinal(part, bounds)
    if (part.byte_start, part.byte_end) != bounds[part.part_index]:
        raise ValueError("joined daily part bounds differ from its original partition")
    expected = part.original_content[part.byte_start:part.byte_end]
    if type(part.content) is not bytes or part.content != expected or sha256_bytes(expected) != part.sha256:
        raise ValueError("joined daily part bytes differ from its original partition")


def _require_contiguous_daily_parts(parts, *, partitions=None):
    first = parts[0]
    bounds, native_ranges = join_partition(first, partitions)
    for part in parts:
        _require_join_identity(part, first)
        require_join_member(part, bounds)
    if any(left.byte_end != right.byte_start for left, right in zip(parts, parts[1:])):
        raise ValueError("joined daily parts have a gap or overlap")
    _require_native_partition_cover(parts, native_ranges)


class DailyPartitionProofs:
    """One packing pass owns pure partition proofs of strongly held immutable bytes."""

    def __init__(self, journal_indexes=None):
        self.sources = {}
        self.journal_indexes = journal_indexes

    def for_part(self, part):
        require_immutable_source(part.original_content)
        key = (id(part.original_content), part.original_sha256)
        if key not in self.sources:
            self.sources[key] = (part.original_content, partition_for_join(part, journal_indexes=self.journal_indexes))
        return self.sources[key][1]


class CapturedPartitions:
    """Pure immutable capture facts, usable only by their original input owner."""

    def __init__(self):
        self.journal_indexes = NativeJournalIndexes()
        self.partitions = DailyPartitionProofs(self.journal_indexes)
        self.owner = None
        self.parts = {}
        self.bound_dailies = None
        self.bound_sources = None

    def daily(self, logical, content, compiled):
        return self._remember_parts(list(self.iter_daily(logical, content, compiled)))

    def iter_daily(self, logical, content, compiled):
        index = self.journal_indexes.for_content(content)
        observed = (verified_native_frame(logical, content, start, head, block, runtime_roots.vault_root())
                    for start, (head, block) in index.items())
        frames = _native_frame_views(content, observed)
        bounds = daily_part_bounds(content)
        spans = tuple(native_frame_spans(frames))
        key = (id(content), immutable_source_digest(content))
        self.partitions.sources[key] = (content, (bounds, spans))
        return iter_daily_parts(logical, content, compiled, native_frames=frames, bounds=bounds)

    def _remember_parts(self, parts):
        self.parts.update((id(part), part) for part in parts)
        return parts

    def bind(self, inputs):
        if any(self.parts.get(id(part)) is not part for part in inputs.dailies):
            raise ValueError("capture partition context does not own these input parts")
        self.owner = weakref.ref(inputs)
        self.bound_dailies, self.bound_sources = inputs.dailies, inputs.sources
        self.journal_indexes.sources.clear()
        return inputs


def measurement_proofs(inputs):
    context = getattr(inputs, "partition_context", None)
    if type(context) is CapturedPartitions and context.owner is not None and context.owner() is inputs:
        return _bound_capture_proofs(context, inputs)
    journal = NativeJournalIndexes()
    return journal, DailyPartitionProofs(journal)


def _bound_capture_proofs(context, inputs):
    journal = NativeJournalIndexes()
    partitions = DailyPartitionProofs(journal)
    if context.bound_dailies is inputs.dailies and context.bound_sources is inputs.sources:
        partitions.sources.update(context.partitions.sources)
    return journal, partitions


def join_partition(part, partitions):
    if partitions is None:
        return _sizing_partition_for_join(part)
    return partitions.for_part(part)


def _sizing_partition_for_join(part):
    """Use only the current sizing pass's canonical immutable partition parser."""
    _journal, partitions = choice_projection_parsing()
    if type(partitions) is DailyPartitionProofs:
        return partitions.for_part(part)
    return partition_for_join(part)


def same_day_parts_join(current_parts, unit, partitions):
    selected = [part for part in current_parts if part.logical_path == unit[0].logical_path]
    try:
        require_native_unit(selected + list(unit), partitions=partitions)
    except ValueError:
        return False
    return True


def _native_prompt_content(parts, raw, *, journal_indexes=None):
    frames = _selected_native_frames(parts, journal_indexes=journal_indexes)
    if not frames:
        return None
    offset = parts[0].byte_start
    result, cursor = [], 0
    for frame in frames:
        start, end = frame.byte_start - offset, frame.byte_end - offset
        result.extend((raw[cursor:start], native_prompt_frame(frame)))
        cursor = end
    result.append(raw[cursor:])
    return b"".join(result)


def overlapping_native_frames(parts):
    first, last = parts[0].byte_start, parts[-1].byte_end
    frames = parts[0].native_frames
    return [frames[index] for index, (start, end) in enumerate(native_frame_spans(frames))
            if start < last and end > first]


def _selected_native_frames(parts, *, journal_indexes=None):
    overlapping = overlapping_native_frames(parts)
    if not overlapping:
        return overlapping
    index = journal_index_for_part(parts[0], journal_indexes)
    for frame in overlapping:
        require_native_frame_cache(frame, parts[0], journal_index=index)
        _require_native_frame_cover(frame, parts)
    return overlapping


def _parse_native_journal(content):
    from fact_keys import native_journal_index

    return native_journal_index(content)


class NativeJournalIndexes:
    """Pure parsing for one owned layout; strong refs prevent identity reuse."""

    def __init__(self):
        self.sources: dict[int, tuple[bytes | VerifiedByteRange, Mapping]] = {}

    def for_content(self, content):
        if type(content) not in (bytes, VerifiedByteRange):
            return _parse_native_journal(content)
        require_immutable_source(content)
        key = id(content)
        if key not in self.sources:
            self.sources[key] = (content, _parse_native_journal(content))
        return self.sources[key][1]


def journal_index_for_part(part, journal_indexes):
    content = physical_source(part).content
    if journal_indexes is None:
        return _parse_native_journal(content)
    return journal_indexes.for_content(content)


def require_native_frame_cache(frame, part, *, journal_index=None):
    physical = physical_source(part)
    start = frame.byte_start - 4
    entry = _available_journal_index(part, journal_index).get(start)
    if entry is None:
        raise ValueError("native frame cache lacks canonical source proof")
    head, block = entry
    verified = verified_native_frame(part.logical_path, physical.content, start, head, block, runtime_roots.vault_root())
    if frame != verified:
        raise ValueError("native frame cache disagrees with canonical source container")


def _available_journal_index(part, journal_index):
    if journal_index is None:
        return _parse_native_journal(physical_source(part).content)
    return journal_index


def _require_native_frame_cover(frame, parts):
    if parts[0].byte_start > frame.byte_start or parts[-1].byte_end < frame.byte_end:
        raise ValueError("native frame requires all covering source parts")
    if any(left.byte_end != right.byte_start for left, right in zip(parts, parts[1:])):
        raise ValueError("native frame source part cover has a gap")


def native_prompt_frame(frame):
    record = json.loads(frame.encoded)
    payload = dict(record["payload"])
    payload.pop("prompt")
    lines = frame.text.splitlines(keepends=True)
    return json.dumps({
        "native_event": {"source_path": frame.source_path, "byte_start": frame.byte_start},
        "evidence_address": {"daily_date": Path(frame.source_path).stem,
                             "timestamp": frame.timestamp,
                             "user_line_indices": list(range(len(lines)))},
        "metadata": {**record, "payload": payload},
        "user_lines": lines,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


# One failure detail on stderr and in the dropped-claims record; the stage and the failure code
# before it are never cut. A readability trade-off, not measured.
MAX_FAILURE_DETAIL_CHARS = 300


def detail_of(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def report_stage_detail(stage: str, failure: str, detail: str) -> None:
    if not detail:
        return
    print(
        f"compile_memory: {stage} {failure}: {detail[:MAX_FAILURE_DETAIL_CHARS]}",
        file=sys.stderr,
    )


def inputs_with_parts(inputs: CompileInputs, parts: tuple) -> CompileInputs:
    """Keep complete selected units and all their available contextual sources."""
    if parts == inputs.dailies:
        return inputs
    omitted = _part_logical_paths(inputs.dailies) - _part_logical_paths(parts)
    return replace(inputs, dailies=parts,
                   sources=select_snapshot_paths(inputs.sources, set(snapshot_paths(inputs.sources)) - omitted))


def _part_logical_paths(parts):
    return {part.logical_path for part in parts}


def choice_projection_parsing():
    return SOURCE_CHOICE_PARSING.get() or (None, None)
