"""Vault answers: search, pages, overview, status, decisions, context, contradictions and compile."""
from __future__ import annotations

import datetime as dt
import functools
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import (
    Path,
    PureWindowsPath,
)

from bounded_io import (
    MAX_KNOWLEDGE_PAGE_BYTES,
    read_stable_bytes,
)
from llm_wiki import mcp_server_limits, mcp_server_workers, runtime_roots
from llm_wiki.mcp_server_limits import (
    _SEARCH_OPERATION_DEADLINE,
    DECISIONS_CALLER,
    LEXICAL_FALLBACK_RESERVE_SECONDS,
    MAX_MCP_EVIDENCE_BYTES,
    MAX_MCP_SLUG_LENGTH,
    MAX_MCP_TOTAL_EVIDENCE_BYTES,
    RECALL_CALLER,
    RETRIEVAL_TRACE_SCHEMA,
    SearchCaller,
)
from llm_wiki.mcp_server_sanitize import (
    _safe_evidence_error,
    _safe_exception_text,
    _safe_page_read_error,
)
from llm_wiki.mcp_server_warmup import warmup_state
from llm_wiki.mcp_server_workers import (
    _call_with_deadline,
    bounded_operation_deadline,
    check_deadline,
)


def search_vault(
    query: str,
    limit: int = 8,
    *,
    deadline: float | None = None,
    trace_sink: dict[str, object] | None = None,
    caller: SearchCaller = RECALL_CALLER,
) -> list[dict]:
    """Run hybrid search on the vault; the planner trace lands in `trace_sink`.

    Every tool that answers from the corpus comes through here, so each gets the
    same deadline reserve, lexical fallback and trace (audit 2026-09-26 C-10).
    """
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    operation_deadline = _search_deadline(deadline)
    # The hybrid run stops a reserved second early, so the lexical fallback has
    # time left to answer in (audit B-18,
    # docs/research/2026-09-25-the-lexical-fallback-keeps-its-own-second.md).
    hybrid_deadline = operation_deadline - LEXICAL_FALLBACK_RESERVE_SECONDS
    try:
        check_deadline(hybrid_deadline)
        return run_vault_search(
            query, limit, hybrid_deadline, semantic=True, trace_sink=trace_sink, caller=caller
        )
    except TimeoutError:
        return _lexical_after_deadline(query, limit, operation_deadline, trace_sink, caller)


def _lexical_after_deadline(
    query: str,
    limit: int,
    operation_deadline: float,
    trace_sink: dict[str, object] | None,
    caller: SearchCaller = RECALL_CALLER,
) -> list[dict]:
    """The hybrid run hit its deadline: one lexical pass, marked as the fallback it is."""
    lexical = run_vault_search(
        query,
        limit,
        operation_deadline,
        semantic=False,
        graph=False,
        rerank=False,
        trace_sink=trace_sink,
        caller=caller,
    )
    if trace_sink is not None:
        trace_sink.update(_lexical_fallback_row({}))
    return [_lexical_fallback_row(row) for row in lexical]


def _search_deadline(deadline: float | None) -> float:
    operation_deadline = (
        _SEARCH_OPERATION_DEADLINE.get() if deadline is None else deadline
    )
    if operation_deadline is None:
        return time.monotonic() + mcp_server_limits.MCP_OPERATION_SECONDS
    return operation_deadline


def run_vault_search(
    query: str,
    limit: int,
    operation_deadline: float,
    *,
    semantic: bool,
    graph: bool = True,
    rerank: bool = True,
    trace_sink: dict[str, object] | None = None,
    caller: SearchCaller = RECALL_CALLER,
) -> list[dict]:
    from search_memory import search

    # No `max_candidates`: that is a per-backend resource cap, not the answer
    # size. Passing the answer size collapsed each leg's pool to the number of
    # rows asked for, and one page owns many chunks -- measured on the live
    # vault, five rows for the quarantine-retry question were three pages, the
    # decision page absent; without the cap it came first.
    return search(
        query,
        limit=limit,
        semantic=semantic,
        graph=graph,
        rerank=rerank,
        source_tool=caller.source_tool,
        emit_telemetry=caller.emit_telemetry,
        deadline_monotonic=operation_deadline,
        trace_sink=trace_sink,
    )


def _lexical_fallback_row(row: dict) -> dict:
    return {
        **row,
        "requested_mode": "HYBRID",
        "fallback_reason": "retrieval_deadline_exceeded",
        "partial": True,
    }


TRACE_KEYS = ("requested_mode", "effective_mode", "signals_used", "corpus_generation")


def reported_trace(reported: Mapping[str, object]) -> dict[str, object]:
    """The trace the search reported, in the envelope's shape."""
    return {
        "schema_version": "retrieval-trace/v1",
        "requested_mode": reported.get("requested_mode"),
        "effective_mode": reported.get("effective_mode"),
        "signals_used": list(reported.get("signals_used", ())),
        "fallback_reason": reported.get("fallback_reason"),
        "corpus_generation": reported.get("corpus_generation") or "legacy",
        "partial": bool(reported.get("partial", False)),
        "reranker_applied": bool(reported.get("reranker_applied", False)),
        "reranker_model_id": reported.get("reranker_model_id"),
        "reranker_model_revision": reported.get("reranker_model_revision"),
        "reranker_depth": reported.get("reranker_depth"),
        "reranker_duration_ms": reported.get("reranker_duration_ms"),
        "reranker_fallback_reason": reported.get("reranker_fallback_reason"),
        "signals_requested": list(reported.get("signals_requested") or ()),
    }


def _row_trace(row: Mapping[str, object]) -> dict[str, object]:
    """A compatibility row carries the trace under `generation`, not `corpus_generation`."""
    return reported_trace({**row, "corpus_generation": row.get("generation")})


def unreported_trace(query: str) -> dict[str, object]:
    """Nothing reported a trace: say so, and never guess a generation."""
    from retrieval import analyze_query, planned_request

    requested, _signals = planned_request(None, analyze_query(query), semantic=True)
    return reported_trace(
        {
            "requested_mode": requested,
            "effective_mode": "BASE",
            "fallback_reason": "trace_unavailable",
            "partial": True,
        }
    )


def _carries_trace(mapping: object, *, generation_key: str) -> bool:
    if not isinstance(mapping, Mapping):
        return False
    return all(key in mapping for key in (*TRACE_KEYS[:3], generation_key))


def retrieval_trace(
    query: str, results: list[dict], reported: Mapping[str, object] | None = None
) -> dict[str, object]:
    """The planner trace: reported by the search itself, else recovered from rows.

    The search reports its trace whether or not it returned rows (#26.1), so
    an empty result names the generation it searched; rows alone are the path
    for callers that predate the sink.
    """
    from llm_wiki.formats.schema import validate_schema

    trace = _selected_retrieval_trace(query, results, reported)
    validate_schema(trace, RETRIEVAL_TRACE_SCHEMA)
    return trace


def _selected_retrieval_trace(query: str, results: list[dict], reported) -> dict:
    if _carries_trace(reported, generation_key="corpus_generation"):
        return reported_trace(reported)
    if results and _carries_trace(results[0], generation_key="generation"):
        return _row_trace(results[0])
    return unreported_trace(query)


def read_page(
    slug: str,
    *,
    emit_telemetry: bool = True,
    resolve_evidence: bool = True,
    deadline: float | None = None,
) -> dict:
    """Read a full page by slug."""

    page_path = _readable_page_path(runtime_roots.vault_root(), slug, deadline)
    if isinstance(page_path, dict):
        return page_path
    content = _page_content(page_path)
    if isinstance(content, dict):
        return content
    return _page_with_evidence(
        runtime_roots.vault_root(),
        runtime_roots.state_root(),
        slug,
        page_path,
        content,
        emit_telemetry=emit_telemetry,
        resolve_evidence=resolve_evidence,
        deadline=deadline,
    )


def _page_with_evidence(
    root,
    state_root,
    slug: str,
    page_path,
    content: str,
    *,
    emit_telemetry: bool,
    resolve_evidence: bool,
    deadline: float | None,
) -> dict:
    """The page and its resolved evidence, or the evidence error envelope."""
    evidence = _page_evidence(root, state_root, content, resolve_evidence, deadline)
    if isinstance(evidence, dict):
        return evidence
    if emit_telemetry:
        # One column, one identity: the page's vault-relative path, never the
        # bare slug two pages can share. See
        # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
        _record_page_reads(page_path.relative_to(root).as_posix(), evidence)
    return {
        "slug": slug,
        "path": str(page_path.relative_to(root)),
        "content": content,
        "evidence": evidence,
    }


def _readable_page_path(root: Path, slug, deadline):
    """Return the page path, or the error dict the caller must hand back."""
    invalid = _invalid_slug_error(slug)
    if invalid is not None:
        return invalid
    page_path = root / "knowledge" / "notes" / f"{slug}.md"
    check_deadline(deadline)
    if not page_path.exists():
        return {"error": f"Page not found: {slug}"}
    return page_path


def _slug_is_pathlike(slug: str) -> bool:
    if slug in {".", ".."}:
        return True
    if "/" in slug or "\\" in slug:
        return True
    return bool(PureWindowsPath(slug).drive) or Path(slug).is_absolute()


def _invalid_slug_error(slug) -> dict | None:
    if not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH:
        return {"error": "Invalid page slug"}
    if _slug_is_pathlike(slug):
        return {"error": f"Invalid page slug: {slug}"}
    return None


def _page_content(page_path: Path):
    """Return the decoded page, or the error dict the caller must hand back."""
    try:
        return read_stable_bytes(
            page_path, MAX_KNOWLEDGE_PAGE_BYTES, label="MCP page"
        ).decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Page read failed: {_safe_page_read_error(exc)}"}


def _require_evidence_within_bounds(resolved, evidence_bytes: int, error_class) -> int:
    if len(resolved.bytes) > MAX_MCP_EVIDENCE_BYTES:
        raise error_class(f"evidence slice exceeds {MAX_MCP_EVIDENCE_BYTES} bytes")
    if evidence_bytes + len(resolved.bytes) > MAX_MCP_TOTAL_EVIDENCE_BYTES:
        raise error_class(
            f"total evidence exceeds {MAX_MCP_TOTAL_EVIDENCE_BYTES} bytes"
        )
    return len(resolved.bytes)


def _resolved_evidence(resolver, candidates, deadline, error_class) -> list:
    """One entry per candidate; a reference that parses must resolve or the page is refused.

    Text that only mentions `daily:` and parses as no reference is named per entry
    instead of refusing the page (audit 2026-09-26 B-17); a real reference whose
    bytes do not match is still a page that cannot be trusted.
    """
    evidence = []
    spent = [0]
    for candidate in candidates:
        check_deadline(deadline)
        evidence.append(_evidence_entry(resolver, candidate, spent, error_class))
    return evidence


def _evidence_entry(resolver, candidate, spent: list[int], error_class) -> dict:
    if isinstance(candidate, str):
        return {"reference": None, "error": "not_an_evidence_reference"}
    return _resolved_entry(resolver.resolve(candidate), candidate, spent, error_class)


def _resolved_entry(resolved, reference, spent: list[int], error_class) -> dict:
    spent[0] += _require_evidence_within_bounds(resolved, spent[0], error_class)
    return {
        "reference": str(reference),
        "sha256": resolved.sha256,
        "text": resolved.bytes.decode("utf-8", errors="strict"),
    }


def _page_evidence(root, state_root, content: str, resolve_evidence: bool, deadline):
    """Resolved evidence, or the error dict the caller must hand back."""
    from evidence_resolver import (
        EvidenceResolutionError,
        EvidenceResolver,
        evidence_candidates,
    )

    if not resolve_evidence:
        return []
    resolver = EvidenceResolver(root, state_root=state_root)
    try:
        return _resolved_evidence(resolver, evidence_candidates(content), deadline, EvidenceResolutionError)
    except (EvidenceResolutionError, OSError, UnicodeDecodeError, ValueError) as exc:
        return {"error": f"Evidence resolution failed: {_safe_evidence_error(exc)}"}


def _page_read_events(make_event, kinds) -> list:
    events = []
    for kind, candidate_id in kinds:
        event = make_event(
            event_kind=kind,
            query=None,
            retrieval_mode="direct",
            candidate_id=candidate_id,
            rank=None,
            generation="legacy",
            source_tool="mcp.read_page",
        )
        if event is not None:
            events.append(event)
    return events


def _record_page_reads(page_path: str, evidence: list) -> None:
    """`page_path` is the page's vault-relative path; an evidence row names its quote."""
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        kinds = [
            ("page_read", page_path),
            *(("evidence_read", item["sha256"]) for item in evidence if "sha256" in item),
        ]
        events = _page_read_events(best_effort_make_event, kinds)
        if events:
            best_effort_record_events(events)
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_telemetry(exc)


def _count_dropped_telemetry(error: BaseException) -> None:
    """A telemetry event that could not be written is counted, not forgotten."""
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error

    record_capture_failure("telemetry_event", describe_error(error), error=error)


def wiki_overview(*, deadline: float | None = None) -> dict:
    """Get vault statistics and the retrieval mode search runs in."""
    from lookup_mode import count_wiki_pages, index_status, search_mode

    check_deadline(deadline)
    count = count_wiki_pages()
    check_deadline(deadline)
    mode = search_mode(index_status())
    return {
        "page_count": count,
        "retrieval_tier": mode,
        "vault_root": str(runtime_roots.vault_root()),
    }


def vault_status(*, deadline: float | None = None) -> dict:
    """Return compile status and current daily-file backlog."""
    from memory_state import file_hash, load_state

    check_deadline(deadline)
    state = load_state()
    compiled = state.get("compiled_daily_hashes", {}) or {}
    return {
        "last_compile": state.get("last_compile_at", "never"),
        "last_compile_status": state.get("last_compile_status", "unknown"),
        "compile_backlog": _compile_backlog(runtime_roots.vault_root(), file_hash, compiled, deadline),
        "warmup": warmup_state(),
        "retrieval_degradations": _retrieval_degradations(),
    }


def _retrieval_degradations() -> dict[str, str]:
    """Why a retrieval stage fell back in this process, by kind (audit M5/M6)."""
    from search_memory import degradation_reasons

    return degradation_reasons()


def _daily_files(root: Path) -> list[Path]:
    from memory_state import daily_logs

    try:
        return daily_logs(root / "knowledge" / "daily")
    except OSError:
        return []


def _daily_needs_compile(daily_path: Path, file_hash, compiled: dict) -> bool:
    try:
        current_hash = file_hash(daily_path)
    except OSError:
        return True
    return compiled.get(daily_path.name) != current_hash


def _compile_backlog(root: Path, file_hash, compiled: dict, deadline) -> int:
    """Past days whose log changed since it was compiled.

    Today's log is still being written and is compiled by the nightly pass, so
    it is not a backlog; counting it made the health resource partial all day
    (audit C-24, docs/research/2026-09-25-today-is-not-a-compile-backlog.md).
    """
    backlog = 0
    for daily_path in _closed_daily_files(root):
        check_deadline(deadline)
        if _daily_needs_compile(daily_path, file_hash, compiled):
            backlog += 1
    return backlog


def _closed_daily_files(root: Path) -> list[Path]:
    open_days = _open_days()
    return [path for path in _daily_files(root) if path.stem not in open_days]


def _open_days() -> set[str]:
    """Today, locally and in UTC: a capture may name its day in either."""
    now = dt.datetime.now(dt.UTC)
    return {now.date().isoformat(), now.astimezone().date().isoformat()}


def get_decisions(
    query: str | None = None,
    limit: int = 10,
    *,
    deadline: float | None = None,
    trace_sink: dict[str, object] | None = None,
) -> list[dict]:
    """Get active decisions from the vault: up to `limit` decision pages, one row each.

    The search is asked for a wider pool than the answer, because the filter to
    decisions runs after it and several chunks of one page are one decision; the
    rows are the agent's shape, as `recall` returns them (audit C-25,
    docs/research/2026-09-25-get-decisions-returns-what-was-asked.md).
    """
    from retrieval import CANDIDATE_FANOUT

    _require_decision_query(query)
    effective_query = query or "decision"
    candidates = search_vault(
        effective_query,
        limit * CANDIDATE_FANOUT,
        deadline=deadline,
        trace_sink=trace_sink,
        caller=DECISIONS_CALLER,
    )
    results = _decision_pages(candidates, limit)
    record_decision_impressions(effective_query, results)
    return results


def _decision_pages(candidates: list[dict], limit: int) -> list[dict]:
    """The first row of each decision page, in search order, shaped for an agent."""
    pages: dict[str, dict] = {}
    for row in candidates:
        if _is_decision_result(row):
            pages.setdefault(row.get("path", ""), {**agent_row(row), "type": "decision"})
    return list(pages.values())[:limit]


def _require_decision_query(query) -> None:
    if query is None:
        return
    if not isinstance(query, str):
        raise ValueError("query must be a string")


def _is_decision_result(result: dict) -> bool:
    if result.get("type") == "decision":
        return True
    return "decision" in result.get("path", "").lower()


def _decision_impression_events(make_event, effective_query: str, results: list) -> list:
    events = []
    for rank, result in enumerate(results, start=1):
        event = make_event(
            event_kind="impression",
            query=effective_query,
            retrieval_mode="decision-filter",
            # One column, one identity: the page's vault-relative path, as every
            # other writer of `retrieval_events` names a page. See
            # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
            candidate_id=result.get("path", ""),
            rank=rank,
            generation="legacy",
            source_tool="mcp.get_decisions",
        )
        if event is not None:
            events.append(event)
    return events


def record_decision_impressions(effective_query: str, results: list) -> None:
    if not results:
        return
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = _decision_impression_events(
            best_effort_make_event, effective_query, results
        )
        if events:
            best_effort_record_events(events)
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_telemetry(exc)


def get_context(
    slugs: list[str],
    include: list[str] | None = None,
    *,
    token_budget: int = 8192,
    deadline: float | None = None,
) -> dict:
    """Return one compiler package under one shared token budget."""
    from corpus_snapshot import collect_corpus

    slugs, include = _validated_context_request(slugs, include, token_budget)
    operation_deadline = bounded_operation_deadline(deadline)
    collected_at = _utc_now_seconds()
    snapshot = collect_corpus(runtime_roots.vault_root(), deadline=operation_deadline)
    selection = _context_selection(snapshot, set(slugs))
    answer = functools.partial(
        _context_answer, snapshot, selection, token_budget, include, collected_at, operation_deadline
    )
    result = _context_within_budget(answer, token_budget)
    _record_context_injections(selection["selected_paths"])
    return result


def _context_answer(snapshot, selection, token_budget, include, collected_at, deadline, text_tokens: int) -> dict:
    """The answer with its text packed into `text_tokens`, counted as the answer is."""
    from answer_budget import BYTES_PER_TOKEN

    compiled = compiled_context(snapshot, selection, text_tokens * BYTES_PER_TOKEN, deadline)
    result = context_result(compiled, snapshot, selection, token_budget, include)
    return {**result, "collected_at": collected_at}


def _context_within_budget(answer, token_budget: int) -> dict:
    """The answer whose whole body, not only its text, fits `token_budget`.

    The text gets the whole budget when the answer then fits; otherwise the largest
    text allowance whose answer fits is searched by halving, because a smaller text
    also lists fewer items (audit 2026-09-27 B-12,
    docs/research/2026-09-27-a-context-answer-fits-the-budget-it-was-given.md).
    """
    whole = _fitting_answer(answer, token_budget, token_budget)
    if whole is not None:
        return whole
    return _largest_fitting_answer(answer, token_budget)


def _largest_fitting_answer(answer, token_budget: int) -> dict:
    best, low, high = None, 1, token_budget - 1
    while low <= high:
        middle = (low + high) // 2
        result = _fitting_answer(answer, middle, token_budget)
        if result is None:
            high = middle - 1
            continue
        best, low = result, middle + 1
    if best is None:
        raise ValueError("token_budget cannot hold this answer's own item list; ask for fewer pages or a larger budget")
    return best


def _fitting_answer(answer, text_tokens: int, token_budget: int) -> dict | None:
    """The answer packed into `text_tokens`, or None when it does not fit the budget."""
    from answer_budget import estimate_tokens
    from context_budget import BudgetExceededError

    try:
        result = answer(text_tokens)
    except BudgetExceededError:
        return None
    return result if estimate_tokens(result) <= token_budget else None


def _utc_now_seconds() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _slug_exceeds_bound(slug) -> bool:
    return not isinstance(slug, str) or len(slug) > MAX_MCP_SLUG_LENGTH


def _require_context_slugs(slugs) -> None:
    if not isinstance(slugs, list) or not slugs:
        raise ValueError("slugs exceed the MCP context bound")
    if any(_slug_exceeds_bound(slug) for slug in slugs):
        raise ValueError("slug exceeds the MCP context bound")


def _require_context_include(include) -> None:
    if not isinstance(include, list):
        raise ValueError("include exceeds the MCP context bound")
    if any(not isinstance(item, str) for item in include):
        raise ValueError("include item exceeds the MCP context bound")


def _require_context_token_budget(token_budget) -> None:
    if isinstance(token_budget, bool) or not isinstance(token_budget, int):
        raise ValueError("token_budget must be a positive integer")
    if token_budget < 1:
        raise ValueError("token_budget must be a positive integer")


def _validated_context_request(slugs, include, token_budget):
    _require_context_slugs(slugs)
    include = include or []
    _require_context_include(include)
    _require_context_token_budget(token_budget)
    return list(dict.fromkeys(slugs)), list(dict.fromkeys(include))


def _source_identities(source) -> set:
    return {
        Path(source.record.relative_path).stem,
        source.record.logical_id,
        source.record.relative_path,
    }


def _source_matches(source, requested: set) -> bool:
    return bool(_source_identities(source) & requested)


def _selected_sources(snapshot, requested: set) -> tuple:
    return tuple(
        source for source in snapshot.sources if _source_matches(source, requested)
    )


def selected_chunks(snapshot, selected_paths: set):
    from corpus_snapshot import select_snapshot_chunks

    return select_snapshot_chunks(snapshot, selected_paths)


def _missing_context_slugs(sources, requested: set) -> list:
    found = set()
    for source in sources:
        found |= _source_identities(source) & requested
    return sorted(requested - found)


def _context_selection(snapshot, requested: set) -> dict:
    sources = _selected_sources(snapshot, requested)
    selected_paths = {source.record.relative_path for source in sources}
    return {
        "sources": sources,
        "selected_paths": selected_paths,
        "missing": _missing_context_slugs(sources, requested),
        "chunks": selected_chunks(snapshot, selected_paths),
    }


def compiled_context(snapshot, selection: dict, token_budget: int, deadline):
    from context_budget import BudgetExceededError, ContextBudget
    from context_compiler import compile_context
    from corpus_snapshot import CorpusSnapshot

    narrow = CorpusSnapshot(
        selection["sources"],
        selection["chunks"],
        snapshot.corpus_sha256,
        snapshot.policy,
        snapshot.collector_version,
        snapshot.extractor_version,
    )
    budget = ContextBudget(None, token_budget, 0, 0)
    shortlist = tuple(source.record.logical_id for source in selection["sources"])
    try:
        return compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(chunk.id for chunk in selection["chunks"]),
            budget=budget,
            deadline=deadline,
        )
    except BudgetExceededError:
        return compile_context(
            narrow,
            shortlist=shortlist,
            evidence_chunk_ids=(),
            budget=budget,
            deadline=deadline,
        )


# What an agent reads about a packed item: where it comes from and what it is. Its
# text is in `text`; `item_id` and `source_sha256` spell the source and its digest
# again, 190 characters an item, and `relevance` is the packer's own bookkeeping
# (audit 2026-09-27 B-12).
_ITEM_FIELDS = (
    "source", "parent_id", "representation", "heading_path", "byte_start", "byte_end",
    "type", "status", "project", "valid_from", "valid_to", "aliases",
)


def _item_descriptor(item: dict) -> dict:
    """The item's fields that say something: no empty value, no parent that is its source."""
    kept = {key: item[key] for key in _ITEM_FIELDS if item.get(key) not in (None, [], ())}
    if kept.get("parent_id") == kept.get("source"):
        kept.pop("parent_id", None)
    return kept


def context_result(compiled, snapshot, selection: dict, token_budget: int, include):
    # The packed text is sent once, in `text`; `items` names each item once, without
    # its text. Six typed copies of the list and three traces named every item up to
    # five times (audit 2026-09-26 B-18, 2026-09-27 B-12,
    # docs/research/2026-09-27-a-context-answer-fits-the-budget-it-was-given.md).
    from answer_budget import estimate_text_tokens

    return {
        "text": compiled.text,
        "packed_tokens": estimate_text_tokens(compiled.text),
        "token_budget": token_budget,
        "corpus_generation": snapshot.corpus_sha256,
        "repo_map": sorted(selection["selected_paths"]),
        "items": [_item_descriptor(asdict(item)) for item in compiled.items],
        "dropped": [asdict(item) for item in compiled.trace.packing.dropped],
        "missing_slugs": selection["missing"],
        "include": include,
    }


def _context_injection_events(make_event, selected_paths: set) -> list:
    events = []
    # One column, one identity: the page's vault-relative path, which is what
    # `selected_paths` already holds, not the stem two pages can share. See
    # `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
    for page_path in sorted(selected_paths):
        event = make_event(
            event_kind="context_injected",
            query=None,
            retrieval_mode="direct",
            candidate_id=page_path,
            rank=None,
            generation="legacy",
            source_tool="mcp.get_context",
        )
        if event is not None:
            events.append(event)
    return events


def _record_context_injections(selected_paths: set) -> None:
    if not selected_paths:
        return
    try:
        from retrieval_telemetry import (
            best_effort_make_event,
            best_effort_record_events,
        )

        events = _context_injection_events(best_effort_make_event, selected_paths)
        if events:
            best_effort_record_events(events)
    except Exception as exc:  # noqa: BLE001 - counted, never silent (audit OPS-21)
        _count_dropped_telemetry(exc)


def assess_contradiction_text(
    claim: str, *, deadline: float | None = None
) -> dict:
    from contradiction_pipeline import assess_text

    check_deadline(deadline)
    return assess_text(claim)


def check_contradiction(claim: str, *, deadline: float | None = None) -> dict:
    """Assess a claim and return evidence, validity, and lifecycle advice."""
    return _call_with_deadline(assess_contradiction_text, claim, deadline=deadline)


def log_decision(
    summary: str, rationale: str = "", *, deadline: float | None = None
) -> dict:
    """Append a decision to the daily log."""
    from daily_log_append import append_daily
    from iso_time import local_now

    slug = "manual-decision"
    now = local_now()
    block = f"\n## [{now.strftime('%H:%M:%S')}] manual decision\n"
    block += f"Trigger: manual\nslug: {slug}\nroot: {runtime_roots.vault_root()}\n\n"
    block += f"Decision: {summary}\n"
    if rationale:
        block += f"Rationale: {rationale}\n"

    try:
        check_deadline(deadline)
        path = append_daily(
            now.strftime("%Y-%m-%d"),
            slug,
            block,
            deadline=bounded_operation_deadline(deadline),
            cancelled=mcp_server_workers.operation_cancelled(),
        )
        return {"status": "logged", "path": str(path)}
    except Exception as e:
        return {"error": _safe_exception_text(e, "mcp.log_decision")}


def trigger_compile(*, deadline: float | None = None) -> dict:
    """Compile pending daily logs under the MCP operation bounds."""
    from compile_memory import run_pending_compile

    operation_deadline = bounded_operation_deadline(deadline)
    check_deadline(operation_deadline)
    returncode = run_pending_compile(
        trigger="manual",
        deadline=operation_deadline,
        cancelled=mcp_server_workers.operation_cancelled(),
    )
    # A non-zero return code is a compile that did not happen. Reporting it as
    # completed told the caller the opposite of what occurred.
    status = "completed" if returncode == 0 else "failed"
    return {"status": status, "returncode": returncode}


# What an agent reads from a recall row: the page and its score. The trace is
# in the envelope once; the per-signal scores are the fusion's own bookkeeping
# (audit M7, docs/research/2026-09-11-a-row-carries-its-page-not-the-trace.md).
AGENT_ROW_FIELDS = (
    "candidate_id",
    "path",
    "title",
    "summary",
    "content",
    "score",
    # One per-signal score stays: `_carries_score` reads it to tell a fused
    # answer from a lexical-only one when no trace was reported.
    "vector_score",
    "fused_score",
    # No `chunk_id`: it is `candidate_id` again, 64 hex characters per row
    # (audit 2026-09-26 C-10).
    "heading_ancestry",
    "project",
    "timestamp",
    "source_sha256",
)


def agent_row(row: Mapping[str, object]) -> dict[str, object]:
    return {key: row[key] for key in AGENT_ROW_FIELDS if key in row}
