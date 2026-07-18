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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from context_budget import (
    ContextBudget,
    ContextItem,
    pack_context,
)
from corpus_snapshot import CapturedSource, CorpusSnapshot, RetrievalChunk

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
class CompilationTrace:
    candidate_count: int
    l0_count: int
    l1_count: int
    l2_count: int
    materializations: tuple[MaterializationTrace, ...]
    generated_context_enabled: bool
    duplicate_stems: tuple[str, ...]


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


def _parse_frontmatter(content: str) -> dict[str, object]:
    """Minimal YAML frontmatter reader covering the fields the compiler needs."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end < 0:
        return {}
    block = content[3:end].strip()
    fields: dict[str, object] = {}
    current_key: str | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(" ") or raw.startswith("\t"):
            value = raw.strip().lstrip("-").strip()
            if current_key is not None and isinstance(fields.get(current_key), list):
                fields[current_key].append(value)  # type: ignore[union-attr]
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip().strip("\"'")
        if value:
            fields[key] = value
        else:
            fields[key] = []
            current_key = key
    return fields


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
        try:
            content_text = source.content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            content_text = source.content.decode("utf-8", errors="replace")
        frontmatter = _parse_frontmatter(content_text)
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


def _metadata_prefix(parent: _Parent) -> str:
    """One-line deterministic prefix carrying every metadata signal."""
    parts: list[str] = [parent.title]
    meta = parent.source.metadata
    record = parent.source.record
    if meta.project:
        parts.append(f"project={meta.project}")
    parts.append(f"type={meta.type}")
    parts.append(f"status={meta.status}")
    if meta.valid_from:
        bound = meta.valid_from
        if meta.valid_to:
            bound = f"{bound}..{meta.valid_to}"
        parts.append(f"valid={bound}")
    if meta.confidence:
        parts.append(f"confidence={meta.confidence}")
    if meta.authority:
        parts.append(f"authority={meta.authority}")
    if parent.aliases:
        parts.append("aliases=" + ", ".join(parent.aliases))
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
    char_count = 0
    overview_lines.append(parent.summary)
    char_count += len(parent.summary)
    for raw in body.splitlines()[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("## history"):
            break
        if char_count + len(stripped) >= 1500:
            overview_lines.append("…(truncated, see full page for more)")
            break
        overview_lines.append(stripped)
        char_count += len(stripped)
    return f"{_metadata_prefix(parent)}\n" + "\n".join(overview_lines)


def _l2_text_small_parent(parent: _Parent) -> str:
    body = parent.source.content.decode("utf-8", errors="replace")
    return f"{_metadata_prefix(parent)}\n{body.rstrip()}"


def _l2_text_heading_subtree(
    parent: _Parent,
    chunk: RetrievalChunk,
    *,
    subtree_char_budget: int,
) -> str:
    body = parent.source.content.decode("utf-8", errors="replace")
    # Find the heading line that introduced this chunk's deepest heading.
    target_heading = chunk.heading_ancestry[-1] if chunk.heading_ancestry else ""
    section_start = chunk.byte_start
    if target_heading:
        needle = target_heading
        # Try to anchor to the actual heading line within the body.
        idx = body.rfind(needle, 0, chunk.byte_start + 1)
        if idx >= 0:
            line_start = body.rfind("\n", 0, idx) + 1
            section_start = line_start

    # End at the next sibling/outer heading, or end of body.
    next_heading = body.find("\n##", section_start + 1)
    if next_heading < 0:
        next_heading = body.find("\n#", section_start + 1)
    section_end = len(body) if next_heading < 0 else next_heading

    # Bounded adjacent context (the next N chars after the section).
    adjacent_end = min(len(body), section_end + ADJACENT_CONTEXT_CHARS)
    section_end = min(adjacent_end, section_start + subtree_char_budget)

    text = body[section_start:section_end]
    return f"{_metadata_prefix(parent)}\n{text.rstrip()}"


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
) -> CompiledItem:
    meta = parent.source.metadata
    record = parent.source.record
    return CompiledItem(
        item_id=f"{representation}:{record.logical_id}:{_short_hash(parent.source)}",
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
        mandatory=False,
        representation=compiled.representation,
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
) -> tuple[CompiledItem, MaterializationReason]:
    body_bytes = len(parent.source.content)
    cited_start = chunk.byte_start if chunk is not None else 0
    cited_end = chunk.byte_end if chunk is not None else body_bytes
    cited_headings = chunk.heading_ancestry if chunk is not None else (parent.title,)
    if body_bytes <= small_parent_chars:
        item = _make_compiled_item(
            parent=parent,
            representation="l2",
            text=_l2_text_small_parent(parent),
            heading_path=cited_headings,
            byte_start=cited_start,
            byte_end=cited_end,
            relevance=DEFAULT_RELEVANCE_L2,
        )
        return item, "small_parent_full"
    if chunk is None:
        # No specific chunk pinned; fall back to small-parent expansion of
        # the leading section so L2 always carries something useful.
        item = _make_compiled_item(
            parent=parent,
            representation="l2",
            text=_l2_text_small_parent(parent),
            heading_path=cited_headings,
            byte_start=cited_start,
            byte_end=cited_end,
            relevance=DEFAULT_RELEVANCE_L2,
        )
        return item, "small_parent_full"
    item = _make_compiled_item(
        parent=parent,
        representation="l2",
        text=_l2_text_heading_subtree(
            parent, chunk, subtree_char_budget=large_parent_subtree_chars
        ),
        heading_path=cited_headings,
        byte_start=cited_start,
        byte_end=cited_end,
        relevance=DEFAULT_RELEVANCE_L2,
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

    parents = _build_parents(snapshot)
    shortlist_set = {str(s) for s in shortlist}
    evidence_set = {str(c) for c in evidence_chunk_ids}
    chunks_by_id: dict[str, RetrievalChunk] = {}
    for chunk in snapshot.chunks:
        chunks_by_id[chunk.id] = chunk

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
    for chunk_id in evidence_set:
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
        )
        compiled_items.append(item)
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

    # 4. Pack under the shared budget.
    active_budget = budget if budget is not None else DEFAULT_BUDGET
    packed_items = list(compiled_items)
    packed_text = "\n\n".join(item.text for item in packed_items)
    packed_tokens = sum(len(item.text.encode("utf-8")) for item in packed_items)

    if budget is not None and packed_tokens > active_budget.available_input_tokens:
        packed = pack_context(
            [_to_context_item(item) for item in compiled_items],
            active_budget,
            emergency_byte_cap=None,
        )
        kept_ids = {item.item_id for item in packed.items}
        packed_items = [item for item in compiled_items if item.item_id in kept_ids]
        packed_text = packed.text
        packed_tokens = packed.packed_tokens

    trace = CompilationTrace(
        candidate_count=len(parents),
        l0_count=sum(1 for i in compiled_items if i.representation == "l0"),
        l1_count=sum(1 for i in compiled_items if i.representation == "l1"),
        l2_count=sum(1 for i in compiled_items if i.representation == "l2"),
        materializations=tuple(materializations),
        generated_context_enabled=bool(generated_context),
        duplicate_stems=duplicate_stems,
    )
    return CompiledContext(
        items=tuple(packed_items),
        text=packed_text,
        trace=trace,
        packed_tokens=packed_tokens,
    )
