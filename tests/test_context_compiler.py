"""Task 15: Adaptive Context Compiler — L0/L1/L2 progressive materialization.

The compiler must:
- contribute L0 (one-line metadata) for every parent as broad candidates,
- promote L1 (orientation overview) for shortlisted parents,
- materialize L2/source spans for final evidence,
- key caches by logical path + source SHA-256 + extractor/model descriptor,
- detect duplicate stems without conflating them,
- keep LLM-generated contextual text disabled by default,
- prefix chunks deterministically with page title, heading ancestry, project,
  type, status, aliases, and validity metadata,
- expand small parent pages in full; expand large pages to the matched heading
  subtree plus bounded adjacent context,
- return a retrieval trace and a materialization trace with every package.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from context_budget import ContextBudget  # noqa: E402
from context_compiler import (  # noqa: E402
    CompilationTrace,
    CompiledContext,
    MaterializationTrace,
    compile_context,
)
from corpus_snapshot import (  # noqa: E402
    CapturedSource,
    CorpusSnapshot,
    RetrievalChunk,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
)


def _source(
    relative_path: str,
    content: bytes,
    *,
    type_name: str = "concept",
    project: str | None = None,
    logical_id: str | None = None,
    status: str = "active",
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence: str = "high",
    authority: str = "user",
    language: str | None = "en",
) -> CapturedSource:
    digest = hashlib.sha256(content).hexdigest()
    return CapturedSource(
        SourceRecord(
            logical_id=logical_id or f"source:{relative_path}",
            relative_path=relative_path,
            sha256=digest,
            size=len(content),
            media_type="text/markdown",
            language=language,
            git_oid=None,
        ),
        SourceMetadata(
            type=type_name,
            project=project,
            authority=authority,
            confidence=confidence,
            status=status,
            valid_from=valid_from,
            valid_to=valid_to,
            language=language,
        ),
        content,
    )


def _chunk(
    source: CapturedSource,
    *,
    chunk_id: str,
    heading_ancestry: tuple[str, ...],
    byte_start: int,
    byte_end: int,
    line_start: int = 1,
    line_end: int = 1,
    text: str | None = None,
) -> RetrievalChunk:
    body = source.content.decode("utf-8", errors="replace")
    return RetrievalChunk(
        id=chunk_id,
        source_id=source.record.logical_id,
        source_path=source.record.relative_path,
        parent_page=source.record.relative_path,
        heading_ancestry=heading_ancestry,
        byte_start=byte_start,
        byte_end=byte_end,
        line_start=line_start,
        line_end=line_end,
        text=text if text is not None else body[byte_start:byte_end],
        source_sha256=source.record.sha256,
        span_sha256=hashlib.sha256(
            body[byte_start:byte_end].encode("utf-8")
        ).hexdigest(),
        type=source.metadata.type,
        project=source.metadata.project,
        authority=source.metadata.authority,
        confidence=source.metadata.confidence,
        status=source.metadata.status,
        valid_from=source.metadata.valid_from,
        valid_to=source.metadata.valid_to,
        language=source.metadata.language,
    )


def _snapshot(
    sources: tuple[CapturedSource, ...],
    chunks: tuple[RetrievalChunk, ...] = (),
) -> CorpusSnapshot:
    policy = SnapshotPolicy((), (), False, None, 100, 1024 * 1024, 1024 * 1024, 100, 100, 8)
    digest = hashlib.sha256(
        b"|".join(s.record.sha256.encode("utf-8") for s in sources)
    ).hexdigest()
    return CorpusSnapshot(sources, chunks, digest, policy)


def _page(
    relative_path: str,
    title: str,
    summary: str,
    body: str,
    **kwargs,
) -> CapturedSource:
    type_name = kwargs.pop("type_name", "concept")
    status = kwargs.pop("status", "active")
    full = (
        f"---\n"
        f"type: {type_name}\n"
        f"status: {status}\n"
        + (f"project: {kwargs['project']}\n" if kwargs.get('project') else "")
        + (f"valid_from: {kwargs['valid_from']}\n" if kwargs.get('valid_from') else "")
        + (f"valid_to: {kwargs['valid_to']}\n" if kwargs.get('valid_to') else "")
        + "---\n\n"
        + f"# {title}\n\n"
        + f"One-sentence summary: {summary}\n\n"
        + body
    ).encode("utf-8")
    return _source(
        relative_path,
        full,
        type_name=type_name,
        status=status,
        **kwargs,
    )


def test_broad_l0_candidates_for_every_parent():
    page_a = _page("a.md", "Alpha", "Alpha summary.", "Body A.")
    page_b = _page("b.md", "Beta", "Beta summary.", "Body B.")
    snapshot = _snapshot((page_a, page_b))

    compiled = compile_context(snapshot)

    l0_items = [i for i in compiled.items if i.representation == "l0"]
    assert len(l0_items) == 2
    assert any("Alpha summary." in i.text for i in l0_items)
    assert any("Beta summary." in i.text for i in l0_items)


def test_l1_promotion_for_shortlisted_parents():
    page = _page("auth.md", "Auth", "Auth summary.", "## Decision\n\nWe chose JWT.\n")
    snapshot = _snapshot((page,))

    compiled = compile_context(snapshot, shortlist=(page.record.logical_id,))

    l1_items = [i for i in compiled.items if i.representation == "l1"]
    assert len(l1_items) == 1
    assert "Auth summary." in l1_items[0].text
    # L1 includes the structured body excerpt.
    assert "Decision" in l1_items[0].text


def test_l2_source_span_materialized_for_final_evidence():
    body = "## Decision\n\nWe chose JWT over sessions for the llm-wiki project.\n"
    page = _page("auth.md", "Auth", "Auth summary.", body)
    start = page.content.index(b"## Decision")
    chunk = _chunk(
        page,
        chunk_id="auth#decision",
        heading_ancestry=("Auth", "Decision"),
        byte_start=start,
        byte_end=len(page.content),
    )
    snapshot = _snapshot((page,), (chunk,))

    compiled = compile_context(
        snapshot,
        shortlist=(page.record.logical_id,),
        evidence_chunk_ids=(chunk.id,),
    )

    l2_items = [i for i in compiled.items if i.representation == "l2"]
    assert len(l2_items) == 1
    assert "JWT" in l2_items[0].text
    # L2 carries authoritative byte ranges from the captured span.
    assert l2_items[0].byte_start == 0
    assert l2_items[0].byte_end == len(page.content)
    assert l2_items[0].source_sha256 == page.record.sha256


def test_source_hash_invalidation_rejects_stale_items():
    page_v1 = _page("auth.md", "Auth", "First summary.", "First body.")
    snapshot_v1 = _snapshot((page_v1,))

    first = compile_context(snapshot_v1)
    first_item_ids = {i.item_id for i in first.items}

    # Same logical_id, different bytes → different source SHA-256.
    page_v2 = _page("auth.md", "Auth", "Second summary.", "Second body.")
    snapshot_v2 = _snapshot((page_v2,))

    second = compile_context(snapshot_v2)
    second_item_ids = {i.item_id for i in second.items}

    assert first_item_ids != second_item_ids
    # Item IDs embed the source hash so cache keys cannot conflate versions.
    assert all(page_v1.record.sha256 not in item_id for item_id in second_item_ids)
    assert any(page_v2.record.sha256 in i.item_id for i in second.items)
    assert any(page_v1.record.sha256 in i.item_id for i in first.items)


def test_duplicate_stems_are_detected_without_conflation():
    page_a = _page("concept.md", "Concept A", "A summary.", "Body A.")
    page_b_dir = "knowledge/notes/sub/concept.md"
    page_b = _source(
        page_b_dir,
        (
            b"---\ntype: concept\nstatus: active\n---\n\n"
            b"# Concept B\n\nOne-sentence summary: B summary.\n\nBody B.\n"
        ),
    )
    snapshot = _snapshot((page_a, page_b))

    compiled = compile_context(snapshot)

    # Both parents contribute their own L0; the duplicate stem is reported
    # in the compilation trace so consumers can disambiguate.
    l0_items = [i for i in compiled.items if i.representation == "l0"]
    assert len(l0_items) == 2
    stems = {Path(i.parent_id).stem for i in l0_items}
    assert stems == {"concept"}
    assert "concept" in compiled.trace.duplicate_stems


def test_generated_context_is_disabled_by_default():
    page = _page("auth.md", "Auth", "Auth summary.", "Body.")
    snapshot = _snapshot((page,))

    compiled = compile_context(snapshot)

    assert compiled.trace.generated_context_enabled is False
    # No contextual prefix claims an LLM-generated contextual sentence.
    for item in compiled.items:
        assert "contextual:" not in item.text.lower()


def test_generated_context_true_is_rejected_until_ablation():
    page = _page("auth.md", "Auth", "Auth summary.", "Body.")

    with pytest.raises(ValueError, match="ablation"):
        compile_context(_snapshot((page,)), generated_context=True)


def test_chunks_carry_deterministic_metadata_prefix():
    page = _page(
        "auth.md",
        "Auth",
        "Auth summary.",
        "Body.",
        type_name="decision",
        project="llm-wiki",
        status="active",
        valid_from="2026-07-15",
    )
    snapshot = _snapshot((page,))

    # L1 is shortlist-driven per the Task 15 contract.
    compiled = compile_context(snapshot, shortlist=(page.record.logical_id,))

    l1 = next(i for i in compiled.items if i.representation == "l1")
    # The prefix surfaces project/type/status/validity deterministically.
    assert "Auth" in l1.text
    assert "decision" in l1.text
    assert "llm-wiki" in l1.text
    assert "active" in l1.text
    assert "2026-07-15" in l1.text


def test_small_parent_pages_expand_in_full():
    body = "Short body.\n"
    page = _page("small.md", "Small", "Small summary.", body)
    chunk = _chunk(
        page,
        chunk_id="small#body",
        heading_ancestry=("Small",),
        byte_start=0,
        byte_end=len(body),
    )
    snapshot = _snapshot((page,), (chunk,))

    compiled = compile_context(
        snapshot,
        shortlist=(page.record.logical_id,),
        evidence_chunk_ids=(chunk.id,),
        small_parent_chars=200,  # body well under the limit
    )

    l2 = next(
        (i for i in compiled.items if i.representation == "l2"), None
    )
    assert l2 is not None
    assert "Short body." in l2.text
    assert any(
        m.reason == "small_parent_full" for m in compiled.trace.materializations
    )


def test_large_parent_pages_expand_to_matched_heading_subtree():
    body = (
        "## Decision\n\nWe chose JWT.\n\n"
        + "## Background\n\n" + ("background " * 200) + "\n\n"
        + "## Alternatives\n\n" + ("alt " * 200) + "\n"
    )
    page = _page("big.md", "Big", "Big summary.", body)
    decision_start = page.content.index(b"## Decision")
    background_start = page.content.index(b"## Background")
    decision_chunk = _chunk(
        page,
        chunk_id="big#decision",
        heading_ancestry=("Big", "Decision"),
        byte_start=decision_start,
        byte_end=background_start,
    )
    snapshot = _snapshot((page,), (decision_chunk,))

    compiled = compile_context(
        snapshot,
        shortlist=(page.record.logical_id,),
        evidence_chunk_ids=(decision_chunk.id,),
        small_parent_chars=10,  # force "large" path
        large_parent_subtree_chars=500,
    )

    l2 = next(i for i in compiled.items if i.representation == "l2")
    assert "JWT" in l2.text
    # The "Alternatives" section lies outside the matched subtree bound.
    assert "Alternatives" not in l2.text
    assert any(
        m.reason == "heading_subtree" for m in compiled.trace.materializations
    )


def test_l2_materialization_uses_utf8_byte_spans_and_heading_level_subtree():
    page = _page(
        "unicode.md",
        "Unicode",
        "Résumé.",
        "## Parent\n\n€ lead\n\n### Child\n\n😀 evidence\n\n### Sibling\n\nkeep sibling out\n\n## Next\n\nstop\n",
    )
    content = page.content
    start = content.index(b"### Child")
    end = content.index(b"### Sibling")
    chunk = _chunk(
        page,
        chunk_id="unicode-child",
        heading_ancestry=("Unicode", "Parent", "Child"),
        byte_start=start,
        byte_end=end,
        text=content[start:end].decode(),
    )

    compiled = compile_context(
        _snapshot((page,), (chunk,)),
        evidence_chunk_ids=(chunk.id,),
        small_parent_chars=1,
        large_parent_subtree_chars=10_000,
    )

    l2 = next(item for item in compiled.items if item.representation == "l2")
    assert "heading=Unicode > Parent > Child" in l2.text
    assert "😀 evidence" in l2.text
    assert "keep sibling out" not in l2.text
    assert page.content[l2.byte_start:l2.byte_end].decode() in l2.text
    assert (l2.byte_start, l2.byte_end) == (start, end)


def test_multiple_evidence_chunks_have_unique_ids_and_sorted_trace():
    page = _page("multi.md", "Multi", "Summary.", "## A\n\none\n## B\n\ntwo\n")
    first_start = page.content.index(b"## A")
    second_start = page.content.index(b"## B")
    chunks = (
        _chunk(page, chunk_id="z", heading_ancestry=("Multi", "A"), byte_start=first_start, byte_end=second_start, text=page.content[first_start:second_start].decode()),
        _chunk(page, chunk_id="a", heading_ancestry=("Multi", "B"), byte_start=second_start, byte_end=len(page.content), text=page.content[second_start:].decode()),
    )

    compiled = compile_context(_snapshot((page,), chunks), evidence_chunk_ids=("z", "a"))

    l2 = [item for item in compiled.items if item.representation == "l2"]
    assert len({item.item_id for item in l2}) == 2
    assert compiled.trace.retrieval.evidence_chunk_ids == ("a", "z")


def test_compilation_returns_retrieval_and_materialization_trace():
    page = _page("auth.md", "Auth", "Auth summary.", "Body.")
    snapshot = _snapshot((page,))

    compiled = compile_context(snapshot)

    assert isinstance(compiled, CompiledContext)
    assert isinstance(compiled.trace, CompilationTrace)
    assert compiled.trace.retrieval.candidate_parent_ids == (page.record.logical_id,)
    assert compiled.trace.packing.packed_item_ids == tuple(i.item_id for i in compiled.items)
    assert compiled.trace.l0_count == 1
    assert all(isinstance(m, MaterializationTrace) for m in compiled.trace.materializations)


def test_budget_packs_compiled_items_under_shared_token_limit():
    big = _page(
        "big.md",
        "Big",
        "Big summary.",
        "Word. " * 5000,
    )
    snapshot = _snapshot((big,))
    budget = ContextBudget(None, max_input_tokens=200, reserved_output_tokens=0, safety_margin_tokens=0)

    compiled = compile_context(snapshot, budget=budget)

    assert compiled.packed_tokens <= budget.available_input_tokens


def test_compiler_enforces_default_budget_even_when_budget_omitted(monkeypatch):
    import context_compiler

    monkeypatch.setattr(context_compiler, "DEFAULT_BUDGET", ContextBudget(None, 10, 0, 0))
    page = _page("large.md", "Large", "summary", "body " * 100)

    compiled = context_compiler.compile_context(_snapshot((page,)))

    assert compiled.packed_tokens <= 10
    assert compiled.trace.packing.dropped_item_ids


def test_aliases_propagated_into_prefix_when_present():
    body = "Body."
    raw = (
        "---\ntype: concept\nstatus: active\naliases:\n  - JWT\n  - JSON Web Token\n---\n\n"
        "# Auth Token\n\nOne-sentence summary: Auth token summary.\n\n" + body
    ).encode("utf-8")
    page = _source("auth.md", raw)
    snapshot = _snapshot((page,))

    # L1 is shortlist-driven; aliases surface in the deterministic prefix.
    compiled = compile_context(snapshot, shortlist=(page.record.logical_id,))

    l1 = next(i for i in compiled.items if i.representation == "l1")
    assert "JWT" in l1.text or "JSON Web Token" in l1.text


def test_shortlist_filters_to_only_requested_parents_for_l1():
    page_a = _page("a.md", "Alpha", "Alpha summary.", "Body A.")
    page_b = _page("b.md", "Beta", "Beta summary.", "Body B.")
    snapshot = _snapshot((page_a, page_b))

    compiled = compile_context(snapshot, shortlist=(page_a.record.logical_id,))

    l1_parents = {i.parent_id for i in compiled.items if i.representation == "l1"}
    assert l1_parents == {page_a.record.relative_path}
    # L0 still emitted for every parent regardless of shortlist.
    l0_parents = {i.parent_id for i in compiled.items if i.representation == "l0"}
    assert l0_parents == {page_a.record.relative_path, page_b.record.relative_path}


def test_compiled_text_joins_items_deterministically():
    page_b = _page("b.md", "Beta", "Beta summary.", "Body.")
    page_a = _page("a.md", "Alpha", "Alpha summary.", "Body.")
    snapshot = _snapshot((page_a, page_b))

    first = compile_context(snapshot)
    second = compile_context(snapshot)

    assert first.text == second.text
    assert first.items == second.items
    assert first.text == "\n\n".join(item.text for item in first.items)
    assert first.trace.packing.packed_item_ids == tuple(
        item.item_id for item in first.items
    )


def test_build_tiers_get_l1_keys_legacy_cache_by_source_hash(tmp_path, monkeypatch):
    """Task 15: legacy L1 cache must invalidate on source byte changes."""
    import build_tiers

    notes = tmp_path / "notes"
    notes.mkdir()
    tiers = tmp_path / "tiers"
    monkeypatch.setattr(build_tiers, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(build_tiers, "TIERS_DIR", tiers)

    page = notes / "auth.md"
    page.write_text(
        "# Auth\n\nOne-sentence summary: first.\n\nBody one.\n",
        encoding="utf-8",
    )
    first_hash = hashlib.sha256(page.read_bytes()).hexdigest()

    build_tiers.write_l1("auth", "FIRST L1", source_sha256=first_hash)
    assert build_tiers.get_l1("auth", source_sha256=first_hash) == "FIRST L1"

    # Edit the page so its source hash changes; the old cache entry must
    # NOT be served for the new hash.
    page.write_text(
        "# Auth\n\nOne-sentence summary: second.\n\nBody two.\n",
        encoding="utf-8",
    )
    second_hash = hashlib.sha256(page.read_bytes()).hexdigest()
    assert first_hash != second_hash
    assert build_tiers.get_l1("auth", source_sha256=second_hash) is None
    assert build_tiers.get_l1("auth", source_sha256=first_hash) == "FIRST L1"


def test_build_advisory_last_decision_returns_slug_and_source_hash(tmp_path, monkeypatch):
    """Task 15: build_advisory must surface slug + source_sha256 so L1 cache
    keys cannot conflate versions (regression for the latent ``last["slug"]``
    KeyError that previously forced the L1 fallback on every call)."""
    import build_advisory

    notes = tmp_path / "notes"
    notes.mkdir()
    decision = notes / "auth.md"
    decision.write_text(
        "---\n"
        "type: decision\n"
        "status: active\n"
        "timestamp: 2026-07-15\n"
        "---\n\n"
        "# Auth Decision\n\n"
        "One-sentence summary: Chose JWT.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(build_advisory, "KNOWLEDGE", notes)
    monkeypatch.setattr(build_advisory, "ROOT", tmp_path)

    last = build_advisory._find_last_decision(None)

    assert last is not None
    assert last["slug"] == "auth"
    assert last["source_sha256"] == hashlib.sha256(decision.read_bytes()).hexdigest()


def test_contextual_retrieval_legacy_cache_path_is_hash_suffixed():
    """Task 15: legacy contextual cache path embeds source hash."""
    import contextual_retrieval

    plain = contextual_retrieval.legacy_context_cache_path("auth")
    assert plain.name == "auth.ctx"

    hashed = contextual_retrieval.legacy_context_cache_path(
        "auth", source_sha256="a" * 64
    )
    assert hashed.name.startswith("auth.")
    assert hashed.name.endswith(".ctx")
    assert hashed.name != "auth.ctx"
