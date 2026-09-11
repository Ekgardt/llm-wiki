"""Task 15: Adaptive Context Compiler.

Materializes L0/L1/L2 representations for each captured source and packs
them into one shared token budget. Designed to be called by the retrieval
planner (Task 11, future), the SessionStart context builder, and the
grounded QA pipeline (Tasks 16–17, not yet integrated).

Design contract (from docs/superpowers/plans/2026-07-16-unified-evidence-retrieval.md):

- L0 is broad ranking metadata (every parent contributes one item).
- L1 is shortlisted orientation (only parents in ``shortlist``).
- L2/source spans are final evidence (only chunks in ``evidence_chunk_ids``).
- Caches key by logical path + source SHA-256 + generator version + model
  descriptor. Item IDs embed the source hash so different versions cannot
  conflate.
- Duplicate stems (e.g. ``foo.md`` and ``sub/foo.md``) are reported, not
  conflated.
- LLM-generated contextual text is OFF by default.
- Chunks carry a deterministic prefix with page title, project, type, status,
  aliases, and validity metadata.
- Small parents expand in full; large parents expand to the matched heading
  subtree plus a bounded adjacent context.
- Every compiled package carries a compilation trace with materializations.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from context_budget import (
    DEFAULT_CONTEXT_BUDGET,
    ContextBudget,
    ContextItem,
    DroppedItem,
    PackedContext,
    pack_context,
)
from corpus_snapshot import (
    CapturedSource,
    CorpusSnapshot,
    RetrievalChunk,
    _frontmatter,
    _markdown_headings,
)

DEFAULT_BUDGET = ContextBudget(
    model=None,
    max_input_tokens=8192,
    reserved_output_tokens=0,
    safety_margin_tokens=512,
)
DEFAULT_SMALL_PARENT_CHARS = 1500
DEFAULT_LARGE_PARENT_SUBTREE_CHARS = 2000
ADJACENT_CONTEXT_CHARS = 200
DEFAULT_RELEVANCE_L0 = 0.4
DEFAULT_RELEVANCE_L1 = 0.7
DEFAULT_RELEVANCE_L2 = 0.95
COMPILER_VERSION = "context-compiler/v1"
LLM_GENERATED_CONTEXT_DEFAULT = False

Representation = Literal["l0", "l1", "l2"]
MaterializationReason = Literal[
    "broad_l0",
    "shortlist_l1",
    "evidence_l2",
    "small_parent_full",
    "heading_subtree",
]


def compile_context_items(
    items: Iterable[ContextItem],
    *,
    budget: ContextBudget = DEFAULT_CONTEXT_BUDGET,
    **packing: object,
) -> PackedContext:
    """Pack final context items through the shared compiler boundary."""
    return pack_context(items, budget, **packing)


@dataclass(frozen=True)
class CompiledItem:
    item_id: str
    text: str
    source: str
    parent_id: str
    representation: Representation
    heading_path: tuple[str, ...]
    byte_start: int
    byte_end: int
    source_sha256: str
    project: str | None
    type: str | None
    status: str | None
    valid_from: str | None
    valid_to: str | None
    aliases: tuple[str, ...]
    relevance: float


@dataclass(frozen=True)
class MaterializationTrace:
    parent_id: str
    representation: Representation
    heading_path: tuple[str, ...]
    byte_start: int
    byte_end: int
    reason: MaterializationReason


@dataclass(frozen=True)
class RetrievalTrace:
    candidate_parent_ids: tuple[str, ...]
    shortlisted_parent_ids: tuple[str, ...]
    evidence_chunk_ids: tuple[str, ...]
    missed_parent_ids: tuple[str, ...]
    missed_evidence_chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class GraphExpansionTrace:
    candidate_id: str
    seed_id: str
    assertion_path: tuple[Mapping[str, object], ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class PackingTrace:
    packed_item_ids: tuple[str, ...]
    dropped: tuple[DroppedItem, ...]
    ranked_item_ids: tuple[str, ...]
    packed_tokens: int
    counter_source: str
    budget_model: str | None

    @property
    def dropped_item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.dropped)


@dataclass(frozen=True)
class CompilationTrace:
    candidate_count: int
    l0_count: int
    l1_count: int
    l2_count: int
    materializations: tuple[MaterializationTrace, ...]
    generated_context_enabled: bool
    duplicate_stems: tuple[str, ...]
    retrieval: RetrievalTrace
    packing: PackingTrace
    graph_expansions: tuple[GraphExpansionTrace, ...] = ()


@dataclass(frozen=True)
class CompiledContext:
    items: tuple[CompiledItem, ...]
    text: str
    trace: CompilationTrace
    packed_tokens: int


@dataclass(frozen=True)
class _Parent:
    source: CapturedSource
    chunks: tuple[RetrievalChunk, ...]
    title: str
    summary: str
    aliases: tuple[str, ...]


def _extract_aliases(frontmatter: Mapping[str, object]) -> tuple[str, ...]:
    aliases = frontmatter.get("aliases")
    if isinstance(aliases, list):
        return tuple(str(a).strip() for a in aliases if str(a).strip())
    return _single_alias(aliases)


def _single_alias(aliases: object) -> tuple[str, ...]:
    if isinstance(aliases, str) and aliases:
        return (aliases,)
    return ()


def _extract_title(content: str, fallback: str) -> str:
    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _extract_summary(content: str) -> str:
    for raw in content.splitlines():
        stripped = raw.strip().lower()
        if stripped.startswith("one-sentence summary:"):
            return raw.split(":", 1)[1].strip()
    return ""


def _build_parents(snapshot: CorpusSnapshot) -> tuple[_Parent, ...]:
    chunks_by_parent: dict[str, list[RetrievalChunk]] = {}
    for chunk in snapshot.chunks:
        chunks_by_parent.setdefault(chunk.parent_page, []).append(chunk)

    parents: list[_Parent] = []
    for source in snapshot.sources:
        relative_path = source.record.relative_path
        content_text = source.content.decode("utf-8", errors="strict")
        frontmatter, _frontmatter_end = _frontmatter(source.content)
        title = _extract_title(content_text, Path(relative_path).stem)
        summary = _extract_summary(content_text) or title
        aliases = _extract_aliases(frontmatter)
        parents.append(
            _Parent(
                source=source,
                chunks=tuple(chunks_by_parent.get(relative_path, ())),
                title=title,
                summary=summary,
                aliases=aliases,
            )
        )
    return tuple(parents)


def _when(value: object, text: str) -> list[str]:
    """[text] when the value is present, [] otherwise."""
    if not value:
        return []
    return [text]


def _metadata_prefix(parent: _Parent, heading_path: tuple[str, ...] = ()) -> str:
    """One-line deterministic prefix carrying every metadata signal."""
    meta = parent.source.metadata
    parts = [
        parent.title,
        *_when(meta.project, f"project={meta.project}"),
        f"type={meta.type}",
        f"status={meta.status}",
        *_when(meta.valid_from, f"valid_from={meta.valid_from}"),
        *_when(meta.valid_to, f"valid_to={meta.valid_to}"),
        *_when(meta.confidence, f"confidence={meta.confidence}"),
        *_when(meta.authority, f"authority={meta.authority}"),
        *_when(parent.aliases, "aliases=" + ", ".join(parent.aliases)),
        *_when(heading_path, "heading=" + " > ".join(heading_path)),
        f"sha256={parent.source.record.sha256[:12]}",
    ]
    return "[" + " | ".join(parts) + "]"


def _detect_duplicate_stems(parents: Iterable[_Parent]) -> tuple[str, ...]:
    by_stem: dict[str, int] = {}
    for parent in parents:
        stem = Path(parent.source.record.relative_path).stem
        by_stem[stem] = by_stem.get(stem, 0) + 1
    return tuple(sorted(stem for stem, count in by_stem.items() if count > 1))


def _l0_text(parent: _Parent) -> str:
    return f"{_metadata_prefix(parent)}\n{parent.summary}"


def _l1_text(parent: _Parent) -> str:
    body = _without_frontmatter(parent.source.content.decode("utf-8", errors="replace"))
    overview_lines = [parent.summary, *_overview_lines(body)]
    return f"{_metadata_prefix(parent)}\n" + "\n".join(overview_lines)


def _without_frontmatter(body: str) -> str:
    if not body.startswith("---"):
        return body
    end = body.find("\n---", 3)
    if end < 0:
        return body
    return body[end + 4:]


def _overview_lines(body: str) -> list[str]:
    """Non-blank body lines after the H1, up to the History section."""
    lines = []
    for raw in body.splitlines()[1:]:
        stripped = raw.strip()
        if stripped.lower().startswith("## history"):
            break
        if stripped:
            lines.append(stripped)
    return lines


def _l2_text_small_parent(parent: _Parent, heading_path: tuple[str, ...]) -> str:
    body = parent.source.content.decode("utf-8", errors="strict")
    return f"{_metadata_prefix(parent, heading_path)}\n{body}"


def _l2_text_heading_subtree(
    parent: _Parent,
    chunk: RetrievalChunk,
    *,
    subtree_char_budget: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[str, int, int]:
    content = parent.source.content
    if not 0 <= chunk.byte_start <= chunk.byte_end <= len(content):
        raise ValueError("evidence chunk byte span is outside its captured source")
    _metadata, searchable_start = _frontmatter(content)
    headings = _markdown_headings(
        content,
        searchable_start,
        deadline=deadline,
        cancelled=cancelled,
    )
    section_start = chunk.byte_start
    section_end = _budgeted_end(
        headings,
        section_start,
        _section_end(headings, chunk, len(content)),
        chunk.byte_end,
        subtree_char_budget,
        len(content),
    )
    span = content[section_start:section_end]
    text = span.decode("utf-8", errors="strict")
    prefix = _metadata_prefix(parent, chunk.heading_ancestry)
    return f"{prefix}\n{text}", section_start, section_end


def _section_end(headings: list, chunk: RetrievalChunk, content_length: int) -> int:
    """The end of the heading subtree the chunk starts; the chunk's own end when no heading starts it."""
    target = _heading_at(headings, chunk.byte_start)
    if target is None:
        return chunk.byte_end
    return _subtree_end(headings, chunk.byte_start, len(target.group(1)), content_length)


def _heading_at(headings: list, start: int):
    return next((match for match in headings if match.start() == start), None)


def _subtree_end(headings: list, start: int, level: int, content_length: int) -> int:
    """Where the next heading of the same or a higher level begins."""
    return next(
        (heading.start() for heading in headings if heading.start() > start and len(heading.group(1)) <= level),
        content_length,
    )


def _budgeted_end(
    headings: list, section_start: int, section_end: int, chunk_end: int, budget: int, content_length: int
) -> int:
    if section_end - section_start > budget:
        return chunk_end
    return _with_adjacent(headings, section_start, section_end, budget, content_length)


def _with_adjacent(headings: list, section_start: int, section_end: int, budget: int, content_length: int) -> int:
    """Extend over a short following section when the whole still fits the budget."""
    adjacent_end = next(
        (heading.start() for heading in headings if heading.start() > section_end),
        content_length,
    )
    adjacent_size = adjacent_end - section_end
    if 0 < adjacent_size <= ADJACENT_CONTEXT_CHARS and adjacent_end - section_start <= budget:
        return adjacent_end
    return section_end


def _short_hash(source: CapturedSource) -> str:
    return source.record.sha256


def _make_compiled_item(
    *,
    parent: _Parent,
    representation: Representation,
    text: str,
    heading_path: tuple[str, ...],
    byte_start: int,
    byte_end: int,
    relevance: float,
    discriminator: str = "",
) -> CompiledItem:
    meta = parent.source.metadata
    record = parent.source.record
    return CompiledItem(
        item_id=(
            f"{representation}:{record.logical_id}:{_short_hash(parent.source)}"
            + (f":{discriminator}" if discriminator else "")
        ),
        text=text,
        source=record.relative_path,
        parent_id=record.relative_path,
        representation=representation,
        heading_path=heading_path,
        byte_start=byte_start,
        byte_end=byte_end,
        source_sha256=record.sha256,
        project=meta.project,
        type=meta.type,
        status=meta.status,
        valid_from=meta.valid_from,
        valid_to=meta.valid_to,
        aliases=parent.aliases,
        relevance=relevance,
    )


def _to_context_item(compiled: CompiledItem) -> ContextItem:
    """Adapt a CompiledItem into the budget packer's ContextItem contract."""
    priority_map = {"l0": 5, "l1": 3, "l2": 2}
    return ContextItem(
        item_id=compiled.item_id,
        text=compiled.text,
        source=compiled.source,
        priority=priority_map.get(compiled.representation, 5),
        relevance=compiled.relevance,
        confidence="high",
        freshness="fresh",
        token_cost=len(compiled.text.encode("utf-8")),
        mandatory=compiled.representation == "l2",
        representation=compiled.representation,
        parent_id=compiled.parent_id,
        priority_class="evidence",
    )


def _build_l0_item(parent: _Parent) -> CompiledItem:
    return _make_compiled_item(
        parent=parent,
        representation="l0",
        text=_l0_text(parent),
        heading_path=(parent.title,),
        byte_start=0,
        byte_end=len(parent.source.content),
        relevance=DEFAULT_RELEVANCE_L0,
    )


def _build_l1_item(parent: _Parent) -> CompiledItem:
    return _make_compiled_item(
        parent=parent,
        representation="l1",
        text=_l1_text(parent),
        heading_path=(parent.title,),
        byte_start=0,
        byte_end=len(parent.source.content),
        relevance=DEFAULT_RELEVANCE_L1,
    )


def _resolve_evidence_chunk(parent: _Parent, chunk_id: str) -> RetrievalChunk | None:
    for chunk in parent.chunks:
        if chunk.id == chunk_id:
            return chunk
    return None


def _build_l2_item(
    parent: _Parent,
    *,
    chunk: RetrievalChunk | None,
    small_parent_chars: int,
    large_parent_subtree_chars: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[CompiledItem, MaterializationReason]:
    if len(parent.source.content) <= small_parent_chars:
        return _whole_parent_l2(parent, chunk), "small_parent_full"
    if chunk is None:
        # No specific chunk pinned; fall back to small-parent expansion of
        # the leading section so L2 always carries something useful.
        return _leading_l2(parent), "small_parent_full"
    return _subtree_l2(parent, chunk, large_parent_subtree_chars, deadline, cancelled), "heading_subtree"


def _whole_parent_l2(parent: _Parent, chunk: RetrievalChunk | None) -> CompiledItem:
    cited_headings = _cited_headings(parent, chunk)
    return _make_compiled_item(
        parent=parent,
        representation="l2",
        text=_l2_text_small_parent(parent, cited_headings),
        heading_path=cited_headings,
        byte_start=0,
        byte_end=len(parent.source.content),
        relevance=DEFAULT_RELEVANCE_L2,
        discriminator=_chunk_discriminator(chunk),
    )


def _cited_headings(parent: _Parent, chunk: RetrievalChunk | None) -> tuple[str, ...]:
    if chunk is None:
        return (parent.title,)
    return chunk.heading_ancestry


def _chunk_discriminator(chunk: RetrievalChunk | None) -> str:
    if chunk is None:
        return "full"
    return chunk.id


def _leading_l2(parent: _Parent) -> CompiledItem:
    cited_headings = (parent.title,)
    return _make_compiled_item(
        parent=parent,
        representation="l2",
        text=_l2_text_small_parent(parent, cited_headings),
        heading_path=cited_headings,
        byte_start=0,
        byte_end=len(parent.source.content),
        relevance=DEFAULT_RELEVANCE_L2,
    )


def _subtree_l2(
    parent: _Parent,
    chunk: RetrievalChunk,
    subtree_char_budget: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> CompiledItem:
    text, emitted_start, emitted_end = _l2_text_heading_subtree(
        parent,
        chunk,
        subtree_char_budget=subtree_char_budget,
        deadline=deadline,
        cancelled=cancelled,
    )
    return _make_compiled_item(
        parent=parent,
        representation="l2",
        text=text,
        heading_path=chunk.heading_ancestry,
        byte_start=emitted_start,
        byte_end=emitted_end,
        relevance=DEFAULT_RELEVANCE_L2,
        discriminator=chunk.id,
    )


def compile_context(
    snapshot: CorpusSnapshot,
    *,
    shortlist: Iterable[str] = (),
    evidence_chunk_ids: Iterable[str] = (),
    budget: ContextBudget | None = None,
    small_parent_chars: int = DEFAULT_SMALL_PARENT_CHARS,
    large_parent_subtree_chars: int = DEFAULT_LARGE_PARENT_SUBTREE_CHARS,
    generated_context: bool = LLM_GENERATED_CONTEXT_DEFAULT,
    graph_expansions: Iterable[Mapping[str, object]] = (),
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> CompiledContext:
    """Compile L0/L1/L2 representations for every parent in ``snapshot``.

    The compiler never silently changes the LLM-generated contextual text
    policy: ``generated_context`` defaults to False and must be explicitly
    enabled by the caller.
    """
    _require_compile_options(snapshot, small_parent_chars, large_parent_subtree_chars, generated_context)
    graph_trace = _graph_expansion_traces(graph_expansions)
    compilation = _Compilation(
        snapshot,
        {str(s) for s in shortlist},
        tuple(sorted({str(c) for c in evidence_chunk_ids})),
    )
    compilation.materialize(
        _L2Limits(small_parent_chars, large_parent_subtree_chars, deadline, cancelled)
    )
    # 4. Always pack under the shared budget, including the default path.
    packed = compile_context_items(
        [_to_context_item(item) for item in compilation.items],
        budget=_budget_or_default(budget),
        per_source_cap=6,
        per_parent_cap=6,
    )
    return compilation.result(packed, graph_trace, generated_context)


def _require_compile_options(
    snapshot: object, small_parent_chars: int, large_parent_subtree_chars: int, generated_context: bool
) -> None:
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    _require_nonnegative_limits(small_parent_chars, large_parent_subtree_chars)
    if generated_context:
        raise ValueError("generated_context=True requires a successful frozen ablation")


def _require_nonnegative_limits(small_parent_chars: int, large_parent_subtree_chars: int) -> None:
    if small_parent_chars < 0:
        raise ValueError("small_parent_chars must be nonnegative")
    if large_parent_subtree_chars < 0:
        raise ValueError("large_parent_subtree_chars must be nonnegative")


def _budget_or_default(budget: ContextBudget | None) -> ContextBudget:
    if budget is None:
        return DEFAULT_BUDGET
    return budget


_INCOMPLETE_PROVENANCE = "graph expansion provenance is incomplete"


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _nonempty_sequence(value: object) -> bool:
    return isinstance(value, (list, tuple)) and bool(value)


def _evidence_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(str(item) for item in values if isinstance(item, str) and item)


def _graph_expansion_traces(graph_expansions: Iterable[Mapping[str, object]]) -> list[GraphExpansionTrace]:
    traces = [_graph_expansion_trace(expansion) for expansion in graph_expansions]
    traces.sort(key=lambda item: (item.candidate_id, item.seed_id))
    return traces


def _graph_expansion_trace(expansion: Mapping[str, object]) -> GraphExpansionTrace:
    candidate_id = expansion.get("candidate_id")
    seed_id = expansion.get("seed_id")
    assertion_path = expansion.get("assertion_path")
    evidence_ids = expansion.get("evidence_ids")
    if not _complete_expansion(candidate_id, seed_id, assertion_path, evidence_ids):
        raise ValueError(_INCOMPLETE_PROVENANCE)
    normalized_evidence = _evidence_strings(evidence_ids)
    normalized_path = [_normalized_step(step) for step in assertion_path]
    if not normalized_evidence:
        raise ValueError(_INCOMPLETE_PROVENANCE)
    return GraphExpansionTrace(
        candidate_id=candidate_id,
        seed_id=seed_id,
        assertion_path=tuple(normalized_path),
        evidence_ids=normalized_evidence,
    )


def _complete_expansion(candidate_id: object, seed_id: object, assertion_path: object, evidence_ids: object) -> bool:
    if not _nonempty_str(candidate_id) or not _nonempty_str(seed_id):
        return False
    return _mapping_path(assertion_path) and _nonempty_sequence(evidence_ids)


def _mapping_path(assertion_path: object) -> bool:
    return _nonempty_sequence(assertion_path) and all(isinstance(step, Mapping) for step in assertion_path)


def _normalized_step(step: Mapping[str, object]) -> dict[str, object]:
    assertion_id = step.get("assertion_id")
    step_evidence = step.get("evidence_ids")
    if not _nonempty_str(assertion_id) or not _nonempty_sequence(step_evidence):
        raise ValueError(_INCOMPLETE_PROVENANCE)
    normalized_step = dict(step)
    normalized_step["evidence_ids"] = _evidence_strings(step_evidence)
    if not normalized_step["evidence_ids"]:
        raise ValueError(_INCOMPLETE_PROVENANCE)
    return normalized_step


@dataclass(frozen=True)
class _L2Limits:
    small_parent_chars: int
    large_parent_subtree_chars: int
    deadline: float | None
    cancelled: Callable[[], bool] | None


def _whole_parent_trace(parent: _Parent, representation: Representation, reason: MaterializationReason) -> MaterializationTrace:
    return MaterializationTrace(
        parent_id=parent.source.record.relative_path,
        representation=representation,
        heading_path=(parent.title,),
        byte_start=0,
        byte_end=len(parent.source.content),
        reason=reason,
    )


def _mapped_ids(packed_items: list[CompiledItem], mapping: dict[str, str]) -> tuple[str, ...]:
    return tuple(sorted(mapping[item.item_id] for item in packed_items if item.item_id in mapping))


def _representation_count(packed_items: list[CompiledItem], representation: str) -> int:
    return sum(1 for item in packed_items if item.representation == representation)


def _packing_trace(packed, packed_items: list[CompiledItem]) -> PackingTrace:
    return PackingTrace(
        packed_item_ids=tuple(item.item_id for item in packed_items),
        dropped=packed.dropped,
        ranked_item_ids=packed.ranked_item_ids,
        packed_tokens=packed.packed_tokens,
        counter_source=packed.counter_source,
        budget_model=packed.budget.model,
    )


class _Compilation:
    """The items one compile materializes, each with its trace and its owner."""

    def __init__(self, snapshot: CorpusSnapshot, shortlist: set[str], evidence_ids: tuple[str, ...]) -> None:
        self.parents = _build_parents(snapshot)
        self.shortlist = shortlist
        self.requested_evidence_ids = evidence_ids
        self.chunks_by_id = {chunk.id: chunk for chunk in snapshot.chunks}
        self.items: list[CompiledItem] = []
        self.materializations: list[MaterializationTrace] = []
        self.l1_parent_by_item_id: dict[str, str] = {}
        self.evidence_by_item_id: dict[str, str] = {}

    def materialize(self, limits: _L2Limits) -> None:
        # 1. Broad L0 for every parent.
        for parent in self.parents:
            self._add(_build_l0_item(parent), _whole_parent_trace(parent, "l0", "broad_l0"))
        # 2. Shortlist L1 promotion.
        for parent in self.parents:
            self._add_shortlisted(parent)
        # 3. Final L2/source evidence.
        for chunk_id in self.requested_evidence_ids:
            self._add_evidence(chunk_id, limits)

    def _add(self, item: CompiledItem, trace: MaterializationTrace) -> None:
        self.items.append(item)
        self.materializations.append(trace)

    def _first_parent_at(self, relative_path: str) -> _Parent | None:
        return next(
            (parent for parent in self.parents if parent.source.record.relative_path == relative_path),
            None,
        )

    def _add_shortlisted(self, parent: _Parent) -> None:
        if parent.source.record.logical_id not in self.shortlist:
            return
        item = _build_l1_item(parent)
        self._add(item, _whole_parent_trace(parent, "l1", "shortlist_l1"))
        owner = self._first_parent_at(item.parent_id)
        self.l1_parent_by_item_id[item.item_id] = owner.source.record.logical_id

    def _add_evidence(self, chunk_id: str, limits: _L2Limits) -> None:
        chunk = self.chunks_by_id.get(chunk_id)
        owner = self._evidence_owner(chunk)
        if owner is None:
            return
        item, reason = _build_l2_item(
            owner,
            chunk=chunk,
            small_parent_chars=limits.small_parent_chars,
            large_parent_subtree_chars=limits.large_parent_subtree_chars,
            deadline=limits.deadline,
            cancelled=limits.cancelled,
        )
        self.evidence_by_item_id[item.item_id] = chunk_id
        self._add(
            item,
            MaterializationTrace(
                parent_id=owner.source.record.relative_path,
                representation="l2",
                heading_path=item.heading_path,
                byte_start=item.byte_start,
                byte_end=item.byte_end,
                reason=reason,
            ),
        )

    def _evidence_owner(self, chunk: RetrievalChunk | None) -> _Parent | None:
        if chunk is None:
            return None
        return self._first_parent_at(chunk.parent_page)

    def missed_parent_ids(self) -> tuple[str, ...]:
        logical_ids = {parent.source.record.logical_id for parent in self.parents}
        return tuple(sorted(self.shortlist - logical_ids))

    def missed_evidence_ids(self) -> tuple[str, ...]:
        parent_paths = {parent.source.record.relative_path for parent in self.parents}
        return tuple(
            chunk_id
            for chunk_id in self.requested_evidence_ids
            if not self._evidence_within(chunk_id, parent_paths)
        )

    def _evidence_within(self, chunk_id: str, parent_paths: set[str]) -> bool:
        chunk = self.chunks_by_id.get(chunk_id)
        return chunk is not None and chunk.parent_page in parent_paths

    def _packed_l0_parent_ids(self, packed_items: list[CompiledItem]) -> tuple[str, ...]:
        l0_paths = {item.parent_id for item in packed_items if item.representation == "l0"}
        return tuple(
            sorted(
                parent.source.record.logical_id
                for parent in self.parents
                if parent.source.record.relative_path in l0_paths
            )
        )

    def _retrieval_trace(self, packed_items: list[CompiledItem]) -> RetrievalTrace:
        return RetrievalTrace(
            candidate_parent_ids=self._packed_l0_parent_ids(packed_items),
            shortlisted_parent_ids=_mapped_ids(packed_items, self.l1_parent_by_item_id),
            evidence_chunk_ids=_mapped_ids(packed_items, self.evidence_by_item_id),
            missed_parent_ids=self.missed_parent_ids(),
            missed_evidence_chunk_ids=self.missed_evidence_ids(),
        )

    def result(self, packed, graph_trace: list[GraphExpansionTrace], generated_context: bool) -> CompiledContext:
        compiled_by_id = {item.item_id: item for item in self.items}
        trace_by_id = {item.item_id: trace for item, trace in zip(self.items, self.materializations)}
        packed_items = [compiled_by_id[item.item_id] for item in packed.items]
        materializations = [trace_by_id[item.item_id] for item in packed.items]
        trace = CompilationTrace(
            candidate_count=len(self.parents),
            l0_count=_representation_count(packed_items, "l0"),
            l1_count=_representation_count(packed_items, "l1"),
            l2_count=_representation_count(packed_items, "l2"),
            materializations=tuple(materializations),
            generated_context_enabled=bool(generated_context),
            duplicate_stems=_detect_duplicate_stems(self.parents),
            retrieval=self._retrieval_trace(packed_items),
            packing=_packing_trace(packed, packed_items),
            graph_expansions=tuple(graph_trace),
        )
        return CompiledContext(
            items=tuple(packed_items),
            text=packed.text,
            trace=trace,
            packed_tokens=packed.packed_tokens,
        )
