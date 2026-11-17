"""Candidate metadata, reciprocal-rank fusion, effective mode and deadline checks."""
from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from typing import Any

from llm_wiki.retrieval_graph import (
    assertion_path_of,
    weigh_by_trust,
)
from llm_wiki.retrieval_query import (
    BM25_WEIGHT,
    DENSE_WEIGHT,
    GRAPH_WEIGHT,
    RRF_K,
    RetrievalCandidate,
    as_float,
    as_int,
    candidate_key,
    first_present,
    heading_path_of,
    hit_path,
    source_sha256,
)
from provenance import curated_pages_first

_GRAPH_META_FIELDS = (
    "graph_seed_id",
    "graph_direction",
    "graph_edge_type",
    "assertion_path",
    "graph_text_overlap",
)

# Display fields any backend may fill; the first non-empty value wins.
_MERGEABLE_META_FIELDS = (
    "title",
    "summary",
    "content",
    "project",
    "timestamp",
    "chunk_id",
    "authority",
    "confidence",
    "status",
    "type",
    "valid_from",
    "valid_to",
    "language",
    "source_id",
    "span_sha256",
    *_GRAPH_META_FIELDS,
)


def evidence_ids_of(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item)
        for item in (row.get("evidence_ids") or ())
        if isinstance(item, str) and item
    )


def _graph_meta(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "graph_seed_id": row.get("seed_id"),
        "graph_direction": row.get("direction"),
        "graph_edge_type": row.get("edge_type"),
        "assertion_path": assertion_path_of(row),
        "graph_text_overlap": row.get("graph_text_overlap"),
    }


def _new_candidate_meta(key: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """The display and provenance record for a candidate seen for the first time."""
    path = hit_path(row)
    meta: dict[str, Any] = {
        "candidate_id": key,
        "parent_id": str(first_present(row, ("parent_id", "parent_page"), path)),
        "relative_path": path,
        "heading_path": heading_path_of(
            first_present(row, ("heading_path", "heading_ancestry"), None)
        ),
        "source_sha256": source_sha256(row),
        "byte_start": as_int(row.get("byte_start"), 0),
        "byte_end": as_int(row.get("byte_end"), 0),
        "bm25_rank": None,
        "bm25_score": None,
        "vector_rank": None,
        "vector_score": None,
        "graph_rank": None,
        "graph_score": None,
        "evidence_ids": evidence_ids_of(row),
    }
    meta.update(
        {
            field: row.get(field)
            for field in ("title", "summary", "project", "timestamp", "authority",
                          "confidence", "status", "type", "valid_from", "valid_to",
                          "language", "source_id", "span_sha256")
        }
    )
    meta["content"] = row.get("content") or row.get("summary")
    meta["chunk_id"] = row.get("chunk_id") or key
    meta.update(_graph_meta(row))
    return meta


_EMPTY_GRAPH_VALUES = (None, "", ())
_EMPTY_DISPLAY_VALUES = (None, "")


def _fill_blank_fields(
    meta: dict[str, Any],
    values: Mapping[str, Any],
    empty: tuple[Any, ...],
) -> None:
    for field, value in values.items():
        if meta.get(field) in empty and value not in empty:
            meta[field] = value


def _merge_evidence_ids(meta: dict[str, Any], row: Mapping[str, Any]) -> None:
    incoming = evidence_ids_of(row)
    if incoming:
        meta["evidence_ids"] = tuple(dict.fromkeys((*meta["evidence_ids"], *incoming)))


def _merge_candidate_meta(meta: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Fill blanks from another backend's view of the same candidate."""
    _fill_blank_fields(meta, _graph_meta(row), _EMPTY_GRAPH_VALUES)
    _merge_evidence_ids(meta, row)
    _fill_blank_fields(
        meta,
        {field: row.get(field) for field in _MERGEABLE_META_FIELDS},
        _EMPTY_DISPLAY_VALUES,
    )


# Per-backend fusion contribution: rank field, score field, raw source field.
# The weights are read per call so a caller can still tune them at runtime.
_FUSION_BACKENDS = (
    ("lexical", "bm25_rank", "bm25_score", "bm25_score"),
    ("dense", "vector_rank", "vector_score", "vector_score"),
    ("graph", "graph_rank", "graph_score", "graph_boost"),
)


def _fusion_weights() -> dict[str, float]:
    return {"lexical": BM25_WEIGHT, "dense": DENSE_WEIGHT, "graph": GRAPH_WEIGHT}


def _raw_backend_score(row: Mapping[str, Any], field: str) -> float | None:
    """The backend's own magnitude, kept for display but never fused."""
    if field in row:
        return as_float(row.get(field))
    return as_float(row.get("score"))


def _accumulate_backend(
    rows: Sequence[Mapping[str, Any]],
    *,
    weight: float,
    rank_field: str,
    score_field: str,
    raw_field: str,
    k: int,
    scores: dict[str, float],
    meta: dict[str, dict[str, Any]],
) -> None:
    for rank, row in enumerate(rows, start=1):
        key = _ensure_candidate_meta(meta, row)
        scores[key] = scores.get(key, 0.0) + weight / (k + rank)
        meta[key][rank_field] = rank
        meta[key][score_field] = _raw_backend_score(row, raw_field)


def _ensure_candidate_meta(meta: dict[str, dict[str, Any]], row: Mapping[str, Any]) -> str:
    key = candidate_key(row)
    if key not in meta:
        meta[key] = _new_candidate_meta(key, row)
        return key
    _merge_candidate_meta(meta[key], row)
    return key


def _fused_candidate(
    key: str, info: Mapping[str, Any], rrf: float, final: float
) -> RetrievalCandidate:
    del key
    return RetrievalCandidate(
        candidate_id=info["candidate_id"],
        parent_id=info["parent_id"],
        relative_path=info["relative_path"],
        heading_path=info["heading_path"],
        source_sha256=info["source_sha256"],
        byte_start=info["byte_start"],
        byte_end=info["byte_end"],
        bm25_rank=info["bm25_rank"],
        bm25_score=info["bm25_score"],
        vector_rank=info["vector_rank"],
        vector_score=info["vector_score"],
        graph_rank=info["graph_rank"],
        graph_score=info["graph_score"],
        rrf_score=rrf,
        rerank_score=None,
        final_score=final,
        evidence_ids=info["evidence_ids"],
        authority_weight=info["authority_weight"],
        type_weight=info["type_weight"],
    )


def fuse_rrf(
    *,
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
    graph: Sequence[Mapping[str, Any]] | None,
    k: int = RRF_K,
    intents: Sequence[str] | None = None,
    query: str | None = None,
) -> tuple[tuple[RetrievalCandidate, ...], dict[str, dict[str, Any]]]:
    """Fuse independent ranked lists with weighted rank-only RRF.

    Larger final_score wins. Raw backend magnitudes are preserved on the
    candidate but never enter the fusion formula. Equal RRF ties break by
    candidate_id ascending.

    `intents` is what the query analysis read. `None` means the caller did not
    analyse the query at all, and then this ranks exactly as it did before the
    trust weight became conditional; an analysed query — including one whose
    analysis found no intent — is answered under the vault's own rule.
    """
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}
    supplied = {"lexical": lexical, "dense": dense, "graph": graph}
    weights = _fusion_weights()
    for name, rank_field, score_field, raw_field in _FUSION_BACKENDS:
        rows = supplied[name]
        if rows:
            _accumulate_backend(
                rows,
                weight=weights[name],
                rank_field=rank_field,
                score_field=score_field,
                raw_field=raw_field,
                k=k,
                scores=scores,
                meta=meta,
            )
    analysed = intents is not None and curated_pages_first(intents)
    weighted = weigh_by_trust(scores, meta, curated_first=analysed, query=query)
    ordered = sorted(weighted, key=lambda item: (-weighted[item], item))
    candidates = [
        _fused_candidate(key, meta[key], round(scores[key], 6), round(weighted[key], 6))
        for key in ordered
    ]
    return tuple(candidates), meta


def _dense_is_missing(ran_dense: bool, dense_available: bool | None) -> bool:
    if dense_available is False:
        return True
    return dense_available is None and not ran_dense


def _dense_signal(
    wanted: Sequence[str], ran_dense: bool, dense_available: bool | None
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the dense backend."""
    if "dense" not in wanted:
        return None, None
    return _dense_backend_signal(ran_dense, dense_available)


def _dense_backend_signal(
    ran_dense: bool, dense_available: bool | None
) -> tuple[str | None, str | None]:
    """What a wanted dense backend reports about itself."""
    if dense_available is True and ran_dense:
        return "dense", None
    if _dense_is_missing(ran_dense, dense_available):
        return None, "dense_unavailable"
    return None, None


def _graph_signal(
    wanted: Sequence[str],
    ran_graph: bool,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str | None, str | None]:
    """(signal, fallback reason) for the graph backend."""
    if "graph" not in wanted:
        return None, None
    if not graph_enabled:
        return None, "graph_disabled"
    return _graph_backend_signal(ran_graph, graph_available)


def _graph_backend_signal(
    ran_graph: bool, graph_available: bool | None
) -> tuple[str | None, str | None]:
    """What a wanted, enabled graph backend reports about itself."""
    if graph_available is True and ran_graph:
        return "graph", None
    return None, "graph_unavailable"


def _effective_for_hybrid(signals: set[str], requested: str) -> str:
    if "dense" in signals:
        return "HYBRID"
    return _lexical_or_requested(signals, requested)


def _effective_for_graph(signals: set[str], requested: str) -> str:
    """A planned GRAPH run also asks for dense, so without the graph it is HYBRID."""
    if "graph" in signals:
        return "GRAPH"
    return _effective_for_hybrid(signals, requested)


def _effective_for_global(signals: set[str], requested: str) -> str:
    if "dense" in signals and "graph" in signals:
        return "GLOBAL"
    if "dense" in signals:
        return "HYBRID"
    return _effective_for_graph(signals, requested)


def _effective_for_graph_profile(signals: set[str], requested: str) -> str:
    """REPO_MAP and IMPACT keep their name only while the graph answered."""
    if "graph" in signals:
        return requested
    return _effective_for_hybrid(signals, requested)


def _lexical_or_requested(signals: set[str], requested: str) -> str:
    if "lexical" in signals:
        return "BASE"
    return requested


def _effective_mode(requested: str, signals: set[str]) -> str:
    resolvers = {
        "HYBRID": _effective_for_hybrid,
        "GRAPH": _effective_for_graph,
        "GLOBAL": _effective_for_global,
        "REPO_MAP": _effective_for_graph_profile,
        "IMPACT": _effective_for_graph_profile,
    }
    resolver = resolvers.get(requested)
    if resolver is not None:
        return resolver(signals, requested)
    if signals:
        return requested
    return "BASE"


def resolve_effective_mode(
    requested: str,
    *,
    wanted: Sequence[str],
    ran_lexical: bool,
    ran_dense: bool,
    ran_graph: bool,
    dense_available: bool | None,
    graph_available: bool | None,
    graph_enabled: bool,
) -> tuple[str, str | None, tuple[str, ...]]:
    """Compute truthful effective mode, signals, and a single fallback reason."""
    dense_signal, fallback = _dense_signal(wanted, ran_dense, dense_available)
    graph_signal, graph_fallback = _graph_signal(
        wanted, ran_graph, graph_available, graph_enabled
    )
    signals = _collected_signals(
        _lexical_signal(wanted, ran_lexical), dense_signal, graph_signal, ran_lexical
    )
    return (
        _effective_mode(requested, set(signals)),
        fallback or graph_fallback,
        tuple(signals),
    )


def _collected_signals(
    lexical: list[str], dense: str | None, graph: str | None, ran_lexical: bool
) -> list[str]:
    """Signals that actually answered; a lexical-only run still says so."""
    signals = [*lexical, *(item for item in (dense, graph) if item is not None)]
    if not signals and ran_lexical:
        return ["lexical"]
    return signals


def _lexical_signal(wanted: Sequence[str], ran_lexical: bool) -> list[str]:
    if "lexical" in wanted and ran_lexical:
        return ["lexical"]
    return []
