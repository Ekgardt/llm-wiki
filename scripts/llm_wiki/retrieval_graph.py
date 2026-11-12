"""Evidence-graph seeds, neighbour expansion, co-activation boosts and trust weighting."""
from __future__ import annotations

import json
import re
from collections.abc import (
    Callable,
    Mapping,
    Sequence,
)
from pathlib import Path
from typing import Any

from llm_wiki.retrieval_query import (
    GRAPH_EDGE_DECAY,
    GRAPH_GLOBAL_LIMIT,
    GRAPH_MAX_HOPS,
    GRAPH_PER_SEED_LIMIT,
    GRAPH_SEED_LIMIT,
    as_int,
    candidate_key,
    check_stopped,
    hit_path,
)
from provenance import (
    authority_weight,
    source_type_weight,
    substance_weight,
)


def graph_seeds(
    lexical: Sequence[Mapping[str, Any]] | None,
    dense: Sequence[Mapping[str, Any]] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Select rank-qualified seeds without comparing backend score magnitudes."""
    selected: dict[str, Mapping[str, Any]] = {}
    for rows in (lexical or (), dense or ()):
        _collect_seeds(selected, rows)
    return tuple(selected.values())[:GRAPH_SEED_LIMIT]


def _collect_seeds(
    selected: dict[str, Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> None:
    for rank, row in enumerate(rows, start=1):
        if _seed_qualifies(row, rank):
            selected.setdefault(candidate_key(row), row)


def _seed_qualifies(row: Mapping[str, Any], rank: int) -> bool:
    """A stated high confidence qualifies; otherwise the rank has to."""
    confidence = row.get("retrieval_confidence")
    if confidence is None:
        return rank <= GRAPH_SEED_LIMIT
    return str(confidence).casefold() == "high"


def query_terms(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE))


def _graph_text_relevance(query: str, row: Mapping[str, Any]) -> tuple[int, int]:
    terms = query_terms(query)
    text = " ".join(
        str(row.get(field) or "")
        for field in ("title", "summary", "content", "relative_path", "path")
    )
    row_terms = query_terms(text)
    return len(terms & row_terms), int(bool(terms) and terms.issubset(row_terms))


def assertion_path_of(row: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_path = row.get("assertion_path")
    if not isinstance(raw_path, (list, tuple)) or not raw_path:
        return ()
    path: list[dict[str, Any]] = []
    for raw_step in raw_path:
        step = _assertion_step(raw_step)
        if step is None:
            return ()
        path.append(step)
    return tuple(path)


def _step_has_identity(raw_step: object) -> bool:
    """A step worth reading at all: a mapping that names its assertion."""
    if not isinstance(raw_step, Mapping):
        return False
    assertion_id = raw_step.get("assertion_id")
    return isinstance(assertion_id, str) and bool(assertion_id)


def _assertion_step(raw_step: object) -> dict[str, Any] | None:
    """One path step, or None when its identity or evidence is unusable."""
    if not _step_has_identity(raw_step):
        return None
    assert isinstance(raw_step, Mapping)
    evidence_ids = _evidence_id_tuple(raw_step.get("evidence_ids"))
    if not evidence_ids:
        return None
    step = dict(raw_step)
    step["evidence_ids"] = evidence_ids
    return step


def _evidence_id_tuple(raw_evidence: object) -> tuple[str, ...]:
    if not isinstance(raw_evidence, (list, tuple)):
        return ()
    return tuple(item for item in raw_evidence if isinstance(item, str) and item)


def _graph_row_is_valid(
    row: Mapping[str, Any],
    path: tuple[dict[str, Any], ...],
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> bool:
    """A usable expansion: in-range hop, allowed edge and direction, real path."""
    hop = as_int(row.get("hop"), 0)
    if not 1 <= hop <= GRAPH_MAX_HOPS:
        return False
    if not _graph_edge_allowed(row, allowed_directions, allowed_edges):
        return False
    return bool(path)


def _graph_edge_allowed(
    row: Mapping[str, Any], allowed_directions: set[str], allowed_edges: set[str]
) -> bool:
    """The row travels an allowed direction along an allowed edge family."""
    return (
        row.get("direction") in allowed_directions
        and row.get("edge_type") in allowed_edges
    )


def _path_evidence_ids(path: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence_id for step in path for evidence_id in step["evidence_ids"]
        )
    )


def _scored_graph_row(
    row: dict[str, Any],
    *,
    query: str,
    direct_graph_query: bool,
    path: tuple[dict[str, Any], ...],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    """Rank key and enriched row, or None when the text says it is unrelated."""
    overlap, full_match = _graph_text_relevance(query, row)
    if overlap == 0 and not direct_graph_query:
        return None
    hop = as_int(row.get("hop"), 0)
    decay = GRAPH_EDGE_DECAY[str(row.get("edge_type"))] ** hop
    row.update(
        assertion_path=path,
        evidence_ids=_path_evidence_ids(path),
        graph_boost=decay,
        graph_text_overlap=overlap,
    )
    key = (-full_match, -overlap, hop, -decay, str(row.get("seed_id")), candidate_key(row))
    return key, row


def _prepared_graph_row(
    raw: Mapping[str, Any],
    backend_rank: int,
    *,
    query: str,
    direct_graph_query: bool,
    seed_ids: set[str],
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    row = dict(raw)
    seed_id = row.get("seed_id")
    if seed_id is None:
        # Independent pre-Task22 graph retrievers remain rank-only inputs.
        return (0, 0, 0, backend_rank, candidate_key(row)), row
    if not isinstance(seed_id, str) or seed_id not in seed_ids:
        return None
    return _validated_graph_row(
        row,
        query=query,
        direct_graph_query=direct_graph_query,
        allowed_directions=allowed_directions,
        allowed_edges=allowed_edges,
    )


def _validated_graph_row(
    row: dict[str, Any],
    *,
    query: str,
    direct_graph_query: bool,
    allowed_directions: set[str],
    allowed_edges: set[str],
) -> tuple[tuple[object, ...], dict[str, Any]] | None:
    """Score a seeded row once its hop, edge, direction and path check out."""
    path = assertion_path_of(row)
    if not _graph_row_is_valid(row, path, allowed_directions, allowed_edges):
        return None
    return _scored_graph_row(
        row, query=query, direct_graph_query=direct_graph_query, path=path
    )


def _merged_assertion_path(
    existing: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[Any, ...]:
    merged = list(existing.get("assertion_path") or ())
    for step in row.get("assertion_path") or ():
        if step not in merged:
            merged.append(step)
    return tuple(merged)


def _merge_graph_duplicate(existing: dict[str, Any], row: Mapping[str, Any]) -> None:
    """Two paths to the same candidate keep both routes and the stronger boost."""
    existing["assertion_path"] = _merged_assertion_path(existing, row)
    existing["evidence_ids"] = tuple(
        dict.fromkeys((*existing.get("evidence_ids", ()), *row.get("evidence_ids", ())))
    )
    existing["graph_boost"] = max(
        float(existing.get("graph_boost") or 0.0),
        float(row.get("graph_boost") or 0.0),
    )


def _accept_graph_row(
    selected: dict[str, dict[str, Any]],
    row: Mapping[str, Any],
    global_limit: int,
) -> bool:
    """Take the row, or merge it into the candidate already chosen."""
    candidate_id = candidate_key(row)
    existing = selected.get(candidate_id)
    if existing is not None:
        _merge_graph_duplicate(existing, row)
        return True
    if len(selected) >= global_limit:
        return False
    selected[candidate_id] = dict(row)
    return True


def _select_graph_hits(
    prepared: list[tuple[tuple[object, ...], dict[str, Any]]],
    *,
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Mapping[str, Any], ...]:
    selected: dict[str, dict[str, Any]] = {}
    per_seed: dict[str, int] = {}
    for _key, row in sorted(prepared, key=lambda item: item[0]):
        seed_id = str(row.get("seed_id") or "")
        if _seed_quota_left(per_seed, seed_id, per_seed_limit):
            _take_graph_row(selected, per_seed, row, seed_id, global_limit)
    return tuple(selected.values())


def _seed_quota_left(per_seed: dict[str, int], seed_id: str, per_seed_limit: int) -> bool:
    if not seed_id:
        return True
    return per_seed.get(seed_id, 0) < per_seed_limit


def _take_graph_row(
    selected: dict[str, dict[str, Any]],
    per_seed: dict[str, int],
    row: Mapping[str, Any],
    seed_id: str,
    global_limit: int,
) -> None:
    if not _accept_graph_row(selected, row, global_limit):
        return
    if seed_id:
        per_seed[seed_id] = per_seed.get(seed_id, 0) + 1


def prepare_graph_hits(
    rows: Sequence[Mapping[str, Any]],
    *,
    query: str,
    requested_profile: str,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int,
    global_limit: int,
) -> tuple[Mapping[str, Any], ...]:
    """Validate, text-rerank, decay, and cap graph expansions deterministically."""
    prepared: list[tuple[tuple[object, ...], dict[str, Any]]] = []
    for backend_rank, raw in enumerate(rows, start=1):
        item = _prepared_graph_row(
            raw,
            backend_rank,
            query=query,
            direct_graph_query=requested_profile == "GRAPH",
            seed_ids={candidate_key(seed) for seed in seeds},
            allowed_directions=set(directions),
            allowed_edges=set(edge_types),
        )
        if item is not None:
            prepared.append(item)
    return _select_graph_hits(
        prepared, per_seed_limit=per_seed_limit, global_limit=global_limit
    )


def _normalized_graph_directions(directions: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(item) for item in directions))
    if not normalized or any(item not in {"in", "out"} for item in normalized):
        raise ValueError("graph directions must contain only in or out")
    return normalized


def _normalized_graph_edges(edge_types: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(str(item) for item in edge_types)))
    if not normalized or len(normalized) > 64:
        raise ValueError("graph edge types must contain between 1 and 64 values")
    return normalized


def _direction_columns(direction: str) -> tuple[str, str]:
    if direction == "out":
        return "source_node_id", "target_node_id"
    return "target_node_id", "source_node_id"


def neighbour_rows(
    graph: Any,
    *,
    seed_path: str,
    direction: str,
    edges: tuple[str, ...],
    per_seed_limit: int,
    deadline_monotonic: float | None,
) -> Any:
    """One evidenced typed hop out of the sealed graph, in deterministic order."""
    source_column, target_column = _direction_columns(direction)
    edge_placeholders = ",".join("?" for _ in edges)
    rows = graph._execute(
        f"""
        SELECT a.assertion_id, a.source_node_id, a.target_node_id,
               a.edge_type, a.confidence, a.authority, a.extractor,
               neighbor.node_id, neighbor.kind, neighbor.metadata_json,
               target_occ.byte_start, target_occ.byte_end,
               target_source.relative_path, target_source.sha256,
               target_source.content,
               e.evidence_id, e.source_id AS evidence_source_id,
               e.byte_start AS evidence_byte_start,
               e.byte_end AS evidence_byte_end, e.span_sha256,
               evidence_source.relative_path AS evidence_relative_path
        FROM assertion a
        JOIN node neighbor ON neighbor.node_id = a.{target_column}
        JOIN occurrence target_occ ON target_occ.node_id = neighbor.node_id
        JOIN source target_source ON target_source.source_id = target_occ.source_id
        JOIN evidence e ON e.assertion_id = a.assertion_id
        JOIN source evidence_source ON evidence_source.source_id = e.source_id
        WHERE a.{source_column} IN (
            SELECT seed_occ.node_id FROM occurrence seed_occ
            JOIN source seed_source ON seed_source.source_id = seed_occ.source_id
            WHERE seed_source.relative_path = ?
          )
          AND a.resolution = 'resolved'
          AND a.target_node_id IS NOT NULL
          AND a.edge_type IN ({edge_placeholders})
          AND target_occ.occurrence_id = (
            SELECT min(chosen.occurrence_id) FROM occurrence chosen
            WHERE chosen.node_id = neighbor.node_id
          )
        ORDER BY a.edge_type, neighbor.node_id, a.assertion_id, e.evidence_id
        LIMIT ?
        """,
        (seed_path, *edges),
        max_rows=min(1000, max(32, per_seed_limit * 16)),
        deadline=deadline_monotonic,
    )
    return rows


def _evidence_record(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": str(row["evidence_id"]),
        "source_id": str(row["evidence_source_id"]),
        "relative_path": str(row["evidence_relative_path"]),
        "byte_start": int(row["evidence_byte_start"]),
        "byte_end": int(row["evidence_byte_end"]),
        "span_sha256": str(row["span_sha256"]),
    }


def _decoded_content(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _assertion_step_record(row: Mapping[str, Any], direction: str) -> dict[str, Any]:
    return {
        "assertion_id": str(row["assertion_id"]),
        "source_node_id": str(row["source_node_id"]),
        "target_node_id": str(row["target_node_id"]),
        "edge_type": str(row["edge_type"]),
        "direction": direction,
        "confidence": str(row["confidence"]),
        "authority": str(row["authority"]),
        "extractor": str(row["extractor"]),
        "evidence_ids": [],
        "evidence": [],
    }


def _neighbour_item(
    row: Mapping[str, Any], *, seed_id: str, direction: str, graph: Any
) -> dict[str, Any]:
    metadata = json.loads(str(row["metadata_json"]))
    relative_path = str(row["relative_path"])
    return {
        "candidate_id": str(row["node_id"]),
        "parent_id": relative_path,
        "relative_path": relative_path,
        "source_sha256": str(row["sha256"]),
        "byte_start": int(row["byte_start"]),
        "byte_end": int(row["byte_end"]),
        "title": metadata.get("name") or Path(relative_path).stem,
        "content": _decoded_content(row["content"]),
        "seed_id": seed_id,
        "hop": 1,
        "direction": direction,
        "edge_type": str(row["edge_type"]),
        "assertion_path": [_assertion_step_record(row, direction)],
        "evidence_ids": [],
        "generation": getattr(graph, "generation_id", None),
    }


def _freeze_neighbour(item: dict[str, Any]) -> dict[str, Any]:
    step = item["assertion_path"][0]
    step["evidence_ids"] = tuple(step["evidence_ids"])
    step["evidence"] = tuple(step["evidence"])
    item["assertion_path"] = tuple(item["assertion_path"])
    item["evidence_ids"] = tuple(item["evidence_ids"])
    return item


def _group_neighbour_rows(
    rows: Any,
    *,
    seed_id: str,
    direction: str,
    graph: Any,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    """One item per (assertion, neighbour), with its evidence collected."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        check_stopped(deadline_monotonic, cancelled)
        key = (str(row["assertion_id"]), str(row["node_id"]))
        item = grouped.get(key)
        if item is None:
            item = _neighbour_item(row, seed_id=seed_id, direction=direction, graph=graph)
            grouped[key] = item
        evidence = _evidence_record(row)
        step = item["assertion_path"][0]
        step["evidence_ids"].append(evidence["evidence_id"])
        step["evidence"].append(evidence)
        item["evidence_ids"].append(evidence["evidence_id"])
    return [_freeze_neighbour(item) for item in grouped.values()]


def _seed_expansion(
    graph: Any,
    seed: Mapping[str, Any],
    *,
    directions: tuple[str, ...],
    edges: tuple[str, ...],
    per_seed_limit: int,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[dict[str, Any]]:
    seed_id = candidate_key(seed)
    seed_path = hit_path(seed)
    seed_results: list[dict[str, Any]] = []
    for direction in directions:
        check_stopped(deadline_monotonic, cancelled)
        rows = neighbour_rows(
            graph,
            seed_path=seed_path,
            direction=direction,
            edges=edges,
            per_seed_limit=per_seed_limit,
            deadline_monotonic=deadline_monotonic,
        )
        seed_results.extend(
            _group_neighbour_rows(
                rows,
                seed_id=seed_id,
                direction=direction,
                graph=graph,
                deadline_monotonic=deadline_monotonic,
                cancelled=cancelled,
            )
        )
    seed_results.sort(
        key=lambda item: (
            str(item["edge_type"]),
            str(item["candidate_id"]),
            str(item["direction"]),
            str(item["assertion_path"][0]["assertion_id"]),
        )
    )
    return seed_results[:per_seed_limit]


def expand_evidence_graph(
    graph: Any,
    *,
    seeds: Sequence[Mapping[str, Any]],
    directions: Sequence[str],
    edge_types: Sequence[str],
    per_seed_limit: int = GRAPH_PER_SEED_LIMIT,
    global_limit: int = GRAPH_GLOBAL_LIMIT,
    deadline_monotonic: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Read one evidenced typed hop from a sealed ``EvidenceGraph`` handle."""
    if not 1 <= per_seed_limit <= 100 or not 1 <= global_limit <= 1000:
        raise ValueError("graph expansion caps are outside supported bounds")
    normalized_directions = _normalized_graph_directions(directions)
    normalized_edges = _normalized_graph_edges(edge_types)
    results: list[Mapping[str, Any]] = []
    for seed in seeds:
        check_stopped(deadline_monotonic, cancelled)
        results.extend(
            _seed_expansion(
                graph,
                seed,
                directions=normalized_directions,
                edges=normalized_edges,
                per_seed_limit=per_seed_limit,
                deadline_monotonic=deadline_monotonic,
                cancelled=cancelled,
            )
        )
        if len(results) >= global_limit:
            break
    return tuple(results[:global_limit])


def standing_disposition(query: str | None) -> dict[str, float]:
    """What past answers say about each page, as a bounded multiplier.

    A grounded answer names its evidence, so a published citation is a verified
    statement that the page carried a claim past every gate. That is a stronger
    signal than a click and needs no propensity model — and until 2026-09-06 it
    was recorded nowhere at all, while 14 806 impressions of what was merely
    *shown* piled up.

    A page nothing knows about gets 1.0 rather than a penalty: absence of
    evidence is not evidence. The boost is capped, because the failure mode is a
    page useful once becoming permanently privileged.
    See `docs/research/2026-09-06-a-signal-stronger-than-a-click.md`.
    """
    try:
        from retrieval_disposition import disposition

        return disposition(query)
    except Exception as exc:  # noqa: BLE001 - named, never silent; the search still answers
        name_dropped("retrieval_disposition", exc)
        return {}


def name_dropped(kind: str, error: BaseException) -> None:
    """A best-effort step that failed is recorded, not forgotten.

    The bounded failure trail `mcp_server` already writes a dropped telemetry
    event to, and doctor reads. Silence made "this ran and found nothing" look
    exactly like "this raised on every call", which is how the key lookup of
    2026-09-16 stayed broken for a day. See
    `docs/research/2026-09-18-the-context-is-built-under-the-same-clock.md`.
    """
    from capture_diagnostics import record_capture_failure
    from secret_redact import describe_error

    record_capture_failure(kind, describe_error(error), error=error)


def co_activation_table() -> dict[str, dict[str, float]]:
    try:
        from co_activation import load

        return load()
    except Exception as exc:  # noqa: BLE001 - named, never silent; the search still answers
        name_dropped("co_activation", exc)
        return {}


def page_name(relative_path: object) -> str:
    return Path(str(relative_path)).stem.casefold()


def neighbour_boosts(
    scores: Mapping[str, float], meta: Mapping[str, Mapping[str, Any]]
) -> dict[str, float]:
    """What the best-ranked page was mentioned alongside, as a multiplier.

    Gated on purpose: only the neighbours of the page that already ranks first
    are raised. Spreading from every candidate to everything it was ever
    mentioned with is the failure the whole spreading-activation literature is
    written about — activation without a gate reaches the graph and means
    nothing. See `docs/research/2026-09-06-what-was-mentioned-together.md`.
    """
    if not scores:
        return {}
    from co_activation import neighbours

    table = co_activation_table()
    if not table:
        return {}
    leader = max(scores, key=lambda key: (scores[key], key))
    return neighbours(page_name(meta[leader].get("relative_path")), table)


def weigh_by_trust(
    scores: Mapping[str, float],
    meta: dict[str, dict[str, Any]],
    *,
    curated_first: bool,
    query: str | None = None,
) -> dict[str, float]:
    """Multiply each fused score by who said it, what the page is, what it did, and whether it is prose.

    Every factor is recorded on the candidate separately, so the ordering can be
    explained by name rather than by one opaque number. `curated_first` is what
    the query analysis decided: a code-shaped question turns the
    curated-knowledge prior off, and everything else keeps it.
    """
    standing = standing_disposition(query)
    alongside = neighbour_boosts(scores, meta)
    weighted: dict[str, float] = {}
    for key, value in scores.items():
        authority = authority_weight(meta[key].get("authority"), meta[key].get("relative_path"))
        page = source_type_weight(
            meta[key].get("type"),
            meta[key].get("relative_path"),
            curated_first=curated_first,
        )
        carried = standing.get(str(meta[key].get("relative_path")), 1.0)
        near = alongside.get(page_name(meta[key].get("relative_path")), 1.0)
        substance = substance_weight(meta[key].get("content"))
        meta[key]["authority_weight"] = authority
        meta[key]["type_weight"] = page
        meta[key]["carried_weight"] = carried
        meta[key]["alongside_weight"] = near
        meta[key]["substance_weight"] = substance
        weighted[key] = value * authority * page * carried * near * substance
    return weighted
