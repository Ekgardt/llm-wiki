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


def _metadata_prefix(parent: _Parent, heading_path: tuple[str, ...] = ()) -> str:
    """One-line deterministic prefix carrying every metadata signal."""
    parts: list[str] = [parent.title]
    meta = parent.source.metadata
    record = parent.source.record
    if meta.project:
        parts.append(f"project={meta.project}")
    parts.append(f"type={meta.type}")
    parts.append(f"status={meta.status}")
    if meta.valid_from:
        parts.append(f"valid_from={meta.valid_from}")
    if meta.valid_to:
        parts.append(f"valid_to={meta.valid_to}")
    if meta.confidence:
        parts.append(f"confidence={meta.confidence}")
    if meta.authority:
        parts.append(f"authority={meta.authority}")
    if parent.aliases:
        parts.append("aliases=" + ", ".join(parent.aliases))
    if heading_path:
        parts.append("heading=" + " > ".join(heading_path))
    parts.append(f"sha256={record.sha256[:12]}")
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
    body = parent.source.content.decode("utf-8", errors="replace")
    frontmatter_end = 0
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end >= 0:
            frontmatter_end = end + 4
    body = body[frontmatter_end:]
    overview_lines: list[str] = []
    overview_lines.append(parent.summary)
    for raw in body.splitlines()[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("## history"):
            break
        overview_lines.append(stripped)
    return f"{_metadata_prefix(parent)}\n" + "\n".join(overview_lines)


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
    target = next((match for match in headings if match.start() == chunk.byte_start), None)
    section_start = chunk.byte_start
    section_end = chunk.byte_end
    within_subtree_budget = True
    if target is not None:
        level = len(target.group(1))
        section_end = len(content)
        for heading in headings:
            if heading.start() <= section_start:
                continue
            if len(heading.group(1)) <= level:
                section_end = heading.start()
                break
    if section_end - section_start > subtree_char_budget:
        section_end = chunk.byte_end
        within_subtree_budget = False
    if within_subtree_budget:
        adjacent_start = section_end
        adjacent_end = next(
            (heading.start() for heading in headings if heading.start() > adjacent_start),
            len(content),
        )
        adjacent_size = adjacent_end - adjacent_start
        if (
            0 < adjacent_size <= ADJACENT_CONTEXT_CHARS
            and adjacent_end - section_start <= subtree_char_budget
        ):
            section_end = adjacent_end
    span = content[section_start:section_end]
    text = span.decode("utf-8", errors="strict")
    prefix = _metadata_prefix(parent, chunk.heading_ancestry)
    return f"{prefix}\n{text}", section_start, section_end


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
    body_bytes = len(parent.source.content)
    cited_start = chunk.byte_start if chunk is not None else 0
    cited_end = chunk.byte_end if chunk is not None else body_bytes
    cited_headings = chunk.heading_ancestry if chunk is not None else (parent.title,)
    if body_bytes <= small_parent_chars:
        item = _make_compiled_item(
            parent=parent,
            representation="l2",
            text=_l2_text_small_parent(parent, cited_headings),
            heading_path=cited_headings,
            byte_start=0,
            byte_end=body_bytes,
            relevance=DEFAULT_RELEVANCE_L2,
            discriminator=chunk.id if chunk is not None else "full",
        )
        return item, "small_parent_full"
    if chunk is None:
        # No specific chunk pinned; fall back to small-parent expansion of
        # the leading section so L2 always carries something useful.
        item = _make_compiled_item(
            parent=parent,
            representation="l2",
            text=_l2_text_small_parent(parent, cited_headings),
            heading_path=cited_headings,
            byte_start=cited_start,
            byte_end=cited_end,
            relevance=DEFAULT_RELEVANCE_L2,
        )
        return item, "small_parent_full"
    text, emitted_start, emitted_end = _l2_text_heading_subtree(
        parent,
        chunk,
        subtree_char_budget=large_parent_subtree_chars,
        deadline=deadline,
        cancelled=cancelled,
    )
    item = _make_compiled_item(
        parent=parent,
        representation="l2",
        text=text,
        heading_path=cited_headings,
        byte_start=emitted_start,
        byte_end=emitted_end,
        relevance=DEFAULT_RELEVANCE_L2,
        discriminator=chunk.id,
    )
    return item, "heading_subtree"


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
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if small_parent_chars < 0:
        raise ValueError("small_parent_chars must be nonnegative")
    if large_parent_subtree_chars < 0:
        raise ValueError("large_parent_subtree_chars must be nonnegative")
    if generated_context:
        raise ValueError("generated_context=True requires a successful frozen ablation")

    graph_trace: list[GraphExpansionTrace] = []
    for expansion in graph_expansions:
        candidate_id = expansion.get("candidate_id")
        seed_id = expansion.get("seed_id")
        assertion_path = expansion.get("assertion_path")
        evidence_ids = expansion.get("evidence_ids")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(seed_id, str)
            or not seed_id
            or not isinstance(assertion_path, (list, tuple))
            or not assertion_path
            or not all(isinstance(step, Mapping) for step in assertion_path)
            or not isinstance(evidence_ids, (list, tuple))
            or not evidence_ids
        ):
            raise ValueError("graph expansion provenance is incomplete")
        normalized_evidence = tuple(
            str(item) for item in evidence_ids if isinstance(item, str) and item
        )
        normalized_path: list[Mapping[str, object]] = []
        for step in assertion_path:
            assertion_id = step.get("assertion_id")
            step_evidence = step.get("evidence_ids")
            if (
                not isinstance(assertion_id, str)
                or not assertion_id
                or not isinstance(step_evidence, (list, tuple))
                or not step_evidence
            ):
                raise ValueError("graph expansion provenance is incomplete")
            normalized_step = dict(step)
            normalized_step["evidence_ids"] = tuple(
                str(item) for item in step_evidence if isinstance(item, str) and item
            )
            if not normalized_step["evidence_ids"]:
                raise ValueError("graph expansion provenance is incomplete")
            normalized_path.append(normalized_step)
        if not normalized_evidence:
            raise ValueError("graph expansion provenance is incomplete")
        graph_trace.append(
            GraphExpansionTrace(
                candidate_id=candidate_id,
                seed_id=seed_id,
                assertion_path=tuple(normalized_path),
                evidence_ids=normalized_evidence,
            )
        )
    graph_trace.sort(key=lambda item: (item.candidate_id, item.seed_id))

    parents = _build_parents(snapshot)
    shortlist_set = {str(s) for s in shortlist}
    requested_evidence_ids = tuple(sorted({str(c) for c in evidence_chunk_ids}))
    chunks_by_id: dict[str, RetrievalChunk] = {}
    for chunk in snapshot.chunks:
        chunks_by_id[chunk.id] = chunk
    parents_by_logical_id = {
        parent.source.record.logical_id: parent for parent in parents
    }
    missed_parent_ids = tuple(sorted(shortlist_set - parents_by_logical_id.keys()))
    parent_paths = {parent.source.record.relative_path for parent in parents}
    missed_evidence_ids = tuple(
        chunk_id
        for chunk_id in requested_evidence_ids
        if chunk_id not in chunks_by_id
        or chunks_by_id[chunk_id].parent_page not in parent_paths
    )

    compiled_items: list[CompiledItem] = []
    materializations: list[MaterializationTrace] = []

    # 1. Broad L0 for every parent.
    for parent in parents:
        compiled_items.append(_build_l0_item(parent))
        materializations.append(
            MaterializationTrace(
                parent_id=parent.source.record.relative_path,
                representation="l0",
                heading_path=(parent.title,),
                byte_start=0,
                byte_end=len(parent.source.content),
                reason="broad_l0",
            )
        )

    # 2. Shortlist L1 promotion.
    for parent in parents:
        if parent.source.record.logical_id not in shortlist_set:
            continue
        compiled_items.append(_build_l1_item(parent))
        materializations.append(
            MaterializationTrace(
                parent_id=parent.source.record.relative_path,
                representation="l1",
                heading_path=(parent.title,),
                byte_start=0,
                byte_end=len(parent.source.content),
                reason="shortlist_l1",
            )
        )

    # 3. Final L2/source evidence.
    l1_parent_by_item_id: dict[str, str] = {}
    evidence_by_item_id: dict[str, str] = {}
    for item in compiled_items:
        if item.representation == "l1":
            owner = next(
                parent for parent in parents if parent.source.record.relative_path == item.parent_id
            )
            l1_parent_by_item_id[item.item_id] = owner.source.record.logical_id

    for chunk_id in requested_evidence_ids:
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            continue
        owner = next(
            (p for p in parents if p.source.record.relative_path == chunk.parent_page),
            None,
        )
        if owner is None:
            continue
        item, reason = _build_l2_item(
            owner,
            chunk=chunk,
            small_parent_chars=small_parent_chars,
            large_parent_subtree_chars=large_parent_subtree_chars,
            deadline=deadline,
            cancelled=cancelled,
        )
        compiled_items.append(item)
        evidence_by_item_id[item.item_id] = chunk_id
        materializations.append(
            MaterializationTrace(
                parent_id=owner.source.record.relative_path,
                representation="l2",
                heading_path=item.heading_path,
                byte_start=item.byte_start,
                byte_end=item.byte_end,
                reason=reason,
            )
        )

    duplicate_stems = _detect_duplicate_stems(parents)

    # 4. Always pack under the shared budget, including the default path.
    active_budget = budget if budget is not None else DEFAULT_BUDGET
    packed = compile_context_items(
        [_to_context_item(item) for item in compiled_items],
        budget=active_budget,
        per_source_cap=6,
        per_parent_cap=6,
    )
    compiled_by_id = {item.item_id: item for item in compiled_items}
    trace_by_id = {
        item.item_id: trace
        for item, trace in zip(compiled_items, materializations)
    }
    packed_items = [compiled_by_id[item.item_id] for item in packed.items]
    materializations = [trace_by_id[item.item_id] for item in packed.items]
    packed_l0_parent_ids = tuple(
        sorted(
            parent.source.record.logical_id
            for parent in parents
            if any(
                item.representation == "l0"
                and item.parent_id == parent.source.record.relative_path
                for item in packed_items
            )
        )
    )
    packed_shortlist_ids = tuple(
        sorted(
            l1_parent_by_item_id[item.item_id]
            for item in packed_items
            if item.item_id in l1_parent_by_item_id
        )
    )
    packed_evidence_ids = tuple(
        sorted(
            evidence_by_item_id[item.item_id]
            for item in packed_items
            if item.item_id in evidence_by_item_id
        )
    )

    trace = CompilationTrace(
        candidate_count=len(parents),
        l0_count=sum(1 for i in packed_items if i.representation == "l0"),
        l1_count=sum(1 for i in packed_items if i.representation == "l1"),
        l2_count=sum(1 for i in packed_items if i.representation == "l2"),
        materializations=tuple(materializations),
        generated_context_enabled=bool(generated_context),
        duplicate_stems=duplicate_stems,
        retrieval=RetrievalTrace(
            candidate_parent_ids=packed_l0_parent_ids,
            shortlisted_parent_ids=packed_shortlist_ids,
            evidence_chunk_ids=packed_evidence_ids,
            missed_parent_ids=missed_parent_ids,
            missed_evidence_chunk_ids=missed_evidence_ids,
        ),
        packing=PackingTrace(
            packed_item_ids=tuple(item.item_id for item in packed_items),
            dropped=packed.dropped,
            ranked_item_ids=packed.ranked_item_ids,
            packed_tokens=packed.packed_tokens,
            counter_source=packed.counter_source,
            budget_model=packed.budget.model,
        ),
        graph_expansions=tuple(graph_trace),
    )
    return CompiledContext(
        items=tuple(packed_items),
        text=packed.text,
        trace=trace,
        packed_tokens=packed.packed_tokens,
    )
