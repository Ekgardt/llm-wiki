"""Load and validate the frozen retrieval-v2 corpus, and build and filter its retrieval candidates."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path, PurePosixPath

from retrieval_v2_contract import (
    FROZEN_CORPUS_IDS,
    MAX_CORPUS_BYTES,
    MAX_SCHEMA_BYTES,
    Candidate,
    QueryScope,
)
from retrieval_v2_metrics import query_scope

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes, validate_schema


def normalized_id(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _assert_unique(values: Iterable[str], label: str) -> None:
    normalized = [normalized_id(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"duplicate normalized {label}")


def _path_has_forbidden_characters(value: str) -> bool:
    return "\\" in value or ":" in value


def _path_is_not_plain(value: str, path: PurePosixPath) -> bool:
    return path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts)


def _path_looks_private(lowered: str) -> bool:
    return any(marker in lowered for marker in ("/users/", "/home/", "private", "personal"))


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value.startswith("synthetic/")
        or _path_has_forbidden_characters(value)
        or _path_is_not_plain(value, path)
        or _path_looks_private(value.casefold())
    ):
        raise ValueError(f"non-public or non-relative synthetic path: {value}")


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label} date: {value}") from exc


def _read_corpus(corpus_path: Path) -> tuple[bytes, dict]:
    try:
        raw = read_stable_bytes(corpus_path, MAX_CORPUS_BYTES, label="retrieval corpus")
        return raw, json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load corpus {corpus_path}: {exc}") from exc


def _read_schema(schema_path: Path) -> bytes:
    try:
        schema_raw = read_stable_bytes(schema_path, MAX_SCHEMA_BYTES, label="retrieval schema")
        json.loads(schema_raw)
        return schema_raw
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load schema {schema_path}: {exc}") from exc


def _validate_corpus_against_schema(corpus: dict, schema_raw: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-schema-") as temporary:
        bounded_schema = Path(temporary) / "schema.json"
        bounded_schema.write_bytes(schema_raw)
        validate_schema(corpus, bounded_schema)


def _require_frozen_corpus(corpus: dict, raw: bytes) -> None:
    if canonical_json_bytes(corpus) + b"\n" != raw:
        raise ValueError("corpus bytes are not canonical and frozen")
    _require_known_corpus_identity(corpus)


def _require_known_corpus_identity(corpus: dict) -> None:
    if corpus["schema_version"] != "retrieval-corpus/v2":
        raise ValueError("unsupported retrieval corpus schema version")
    if corpus["corpus_id"] not in FROZEN_CORPUS_IDS:
        raise ValueError("unexpected frozen corpus id")


def _require_frozen_order(documents: list[dict], queries: list[dict]) -> None:
    if documents != sorted(documents, key=lambda item: item["parent_id"]):
        raise ValueError("documents must be frozen in parent_id order")
    if queries != sorted(queries, key=lambda item: item["query_id"]):
        raise ValueError("queries must be frozen in query_id order")
    _assert_unique((document["parent_id"] for document in documents), "parent id")
    _assert_unique((query["query_id"] for query in queries), "query id")


def _require_validity_interval(document: dict) -> None:
    parent_id = document["parent_id"]
    valid_from = _parse_date(document["valid_from"], f"{parent_id} valid_from")
    if document["valid_to"] is None:
        return
    valid_to = _parse_date(document["valid_to"], f"{parent_id} valid_to")
    if valid_from >= valid_to:
        raise ValueError(f"invalid validity interval for {parent_id}")


def _require_superseded_lifecycle(document: dict) -> None:
    if document["valid_to"] is None or document["superseded_by"] is None:
        raise ValueError(f"superseded parent lacks lifecycle metadata: {document['parent_id']}")


def _require_lifecycle(document: dict) -> None:
    if document["status"] != "active":
        _require_superseded_lifecycle(document)
        return
    if document["valid_to"] is not None or document["superseded_by"] is not None:
        raise ValueError(f"active parent has supersession metadata: {document['parent_id']}")


def _require_unique_evidence_id(evidence_id: str, evidence_by_id: dict) -> None:
    if normalized_id(evidence_id) in {normalized_id(existing) for existing in evidence_by_id}:
        raise ValueError(f"duplicate normalized evidence id: {evidence_id}")


def _span_text(span: dict, encoded: bytes) -> str:
    evidence_id = span["evidence_id"]
    start = span["byte_start"]
    end = span["byte_end"]
    if start >= end or end > len(encoded):
        raise ValueError(f"invalid UTF-8 range for {evidence_id}")
    try:
        return encoded[start:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"range splits UTF-8 for {evidence_id}") from exc


def _require_span_text(span: dict, selected: str) -> None:
    evidence_id = span["evidence_id"]
    if selected != span["text"]:
        raise ValueError(f"UTF-8 range text mismatch for {evidence_id}")
    if hashlib.sha256(span["text"].encode("utf-8")).hexdigest() != span["span_sha256"]:
        raise ValueError(f"span hash mismatch for {evidence_id}")


def _require_disjoint_span(span: dict, ranges: list[tuple[int, int]], parent_id: str) -> None:
    start = span["byte_start"]
    end = span["byte_end"]
    if any(start < other_end and other_start < end for other_start, other_end in ranges):
        raise ValueError(f"overlapping evidence spans in {parent_id}")


def _register_spans(document: dict, evidence_by_id: dict[str, tuple[dict, dict]]) -> None:
    encoded = document["parent_text"].encode("utf-8")
    ranges: list[tuple[int, int]] = []
    for span in document["evidence_spans"]:
        _require_unique_evidence_id(span["evidence_id"], evidence_by_id)
        _require_span_text(span, _span_text(span, encoded))
        _require_disjoint_span(span, ranges, document["parent_id"])
        ranges.append((span["byte_start"], span["byte_end"]))
        evidence_by_id[span["evidence_id"]] = (document, span)


def _register_document(document: dict, evidence_by_id: dict[str, tuple[dict, dict]]) -> None:
    _validate_relative_path(document["relative_path"])
    _require_validity_interval(document)
    _require_lifecycle(document)
    _register_spans(document, evidence_by_id)


def _require_supersession_targets(documents: list[dict], parent_by_id: dict[str, dict]) -> None:
    for document in documents:
        target = document["superseded_by"]
        if target is None:
            continue
        if target not in parent_by_id or parent_by_id[target]["status"] != "active":
            raise ValueError(f"invalid supersession target for {document['parent_id']}")


def _require_unanswerable_gold(query: dict, gold_fields: tuple) -> None:
    if any(gold_fields) or query["allowed_abstention_reason"] is None:
        raise ValueError(f"invalid unanswerable gold contract: {query['query_id']}")


def _require_gold_contract(query: dict) -> None:
    gold_fields = (query["relevant_parents"], query["required_evidence_spans"], query["graded_evidence"])
    if query["answerability"] != "answerable":
        _require_unanswerable_gold(query, gold_fields)
        return
    if not all(gold_fields) or query["allowed_abstention_reason"] is not None:
        raise ValueError(f"invalid answerable gold contract: {query['query_id']}")


def _require_current_scope(query: dict, scope: dict) -> None:
    if scope["as_of"] is not None:
        raise ValueError(f"current query has as_of: {query['query_id']}")


def _require_temporal_scope(query: dict) -> None:
    scope = query["temporal_scope"]
    if scope["mode"] == "current":
        _require_current_scope(query, scope)
        return
    if scope["as_of"] is None:
        raise ValueError(f"historical query lacks as_of: {query['query_id']}")
    _parse_date(scope["as_of"], f"{query['query_id']} as_of")


def _require_unique_query_fields(query: dict) -> None:
    for field in ("relevant_parents", "required_evidence_spans", "negative_candidates"):
        _assert_unique(query[field], field.replace("_", " "))
    _assert_unique((item["evidence_id"] for item in query["graded_evidence"]), "graded evidence")


class _GoldSets:
    def __init__(self, query: dict) -> None:
        self.relevant_parents = set(query["relevant_parents"])
        self.required_evidence = set(query["required_evidence_spans"])
        self.graded_evidence = {item["evidence_id"] for item in query["graded_evidence"]}
        self.negatives = set(query["negative_candidates"])


def _require_known_gold(query: dict, gold: _GoldSets, parent_by_id: dict, evidence_by_id: dict) -> None:
    query_id = query["query_id"]
    if not gold.relevant_parents <= parent_by_id.keys():
        raise ValueError(f"unknown relevant parent: {query_id}")
    _require_known_gold_evidence(query_id, gold, evidence_by_id)


def _require_known_gold_evidence(query_id: str, gold: _GoldSets, evidence_by_id: dict) -> None:
    known = evidence_by_id.keys()
    if not gold.required_evidence <= known or not gold.graded_evidence <= known:
        raise ValueError(f"unknown relevant evidence: {query_id}")
    if not gold.negatives <= known:
        raise ValueError(f"unknown negative evidence: {query_id}")


def _required_parents(gold: _GoldSets, evidence_by_id: dict) -> set[str]:
    return {evidence_by_id[evidence_id][0]["parent_id"] for evidence_id in gold.required_evidence}


def _require_consistent_gold(query: dict, gold: _GoldSets, evidence_by_id: dict) -> None:
    query_id = query["query_id"]
    if gold.required_evidence & gold.negatives or gold.graded_evidence & gold.negatives:
        raise ValueError(f"positive and negative evidence overlap: {query_id}")
    _require_matching_gold_sets(query_id, gold, evidence_by_id)


def _require_matching_gold_sets(query_id: str, gold: _GoldSets, evidence_by_id: dict) -> None:
    if gold.required_evidence != gold.graded_evidence:
        raise ValueError(f"required and graded evidence differ: {query_id}")
    if gold.relevant_parents != _required_parents(gold, evidence_by_id):
        raise ValueError(f"relevant parents must exactly match required evidence parents: {query_id}")


def _require_evidence_in_scope(query: dict, evidence_id: str, document: dict, gold: _GoldSets, scoped: set) -> None:
    query_id = query["query_id"]
    if document["parent_id"] not in gold.relevant_parents:
        raise ValueError(f"evidence parent is not relevant: {query_id}")
    _require_gold_in_query_scope(query, evidence_id, document, scoped)


def _require_gold_in_query_scope(query: dict, evidence_id: str, document: dict, scoped: set) -> None:
    query_id = query["query_id"]
    if document["project"] not in query["project_scope"]:
        raise ValueError(f"project scope excludes gold: {query_id}")
    if evidence_id not in scoped:
        raise ValueError(f"temporal scope excludes gold: {query_id}")


def _require_scope_covers_gold(query: dict, gold: _GoldSets, evidence_by_id: dict, corpus: dict) -> None:
    scope = query_scope(query)
    scoped = {candidate.evidence_id for candidate in filter_candidates(build_candidates(corpus), scope)}
    for evidence_id in gold.required_evidence:
        document, _span = evidence_by_id[evidence_id]
        _require_evidence_in_scope(query, evidence_id, document, gold, scoped)
    if not gold.negatives & scoped:
        raise ValueError(f"query lacks an eligible negative candidate: {query['query_id']}")


def _require_query(query: dict, parent_by_id: dict, evidence_by_id: dict, corpus: dict) -> None:
    _require_gold_contract(query)
    _require_temporal_scope(query)
    _require_unique_query_fields(query)
    gold = _GoldSets(query)
    _require_known_gold(query, gold, parent_by_id, evidence_by_id)
    _require_consistent_gold(query, gold, evidence_by_id)
    _require_scope_covers_gold(query, gold, evidence_by_id, corpus)


def _language_tallies(queries: list[dict]) -> tuple[dict[str, int], dict[str, set[str]]]:
    language_counts = {language: 0 for language in ("EN", "RU", "ZH")}
    language_answers: dict[str, set[str]] = {language: set() for language in language_counts}
    for query in queries:
        language_counts[query["language"]] += 1
        language_answers[query["language"]].add(query["answerability"])
    return language_counts, language_answers


def _require_cross_language_slice(queries: list[dict]) -> None:
    if sum(query["cross_language"] for query in queries) < 3:
        raise ValueError("cross-language slice requires at least three queries")


def _require_language_coverage(queries: list[dict]) -> None:
    language_counts, language_answers = _language_tallies(queries)
    if any(count < 6 for count in language_counts.values()):
        raise ValueError("each native language requires at least six queries")
    if any(values != {"answerable", "unanswerable"} for values in language_answers.values()):
        raise ValueError("each native language requires answerable and no-answer cases")
    _require_cross_language_slice(queries)


def load_corpus(corpus_path: Path | str, schema_path: Path | str) -> dict:
    """Load and fail closed on schema, frozen-byte, and cross-reference errors."""
    raw, corpus = _read_corpus(Path(corpus_path))
    schema_raw = _read_schema(Path(schema_path))
    _validate_corpus_against_schema(corpus, schema_raw)
    _require_frozen_corpus(corpus, raw)
    documents = corpus["documents"]
    queries = corpus["queries"]
    _require_frozen_order(documents, queries)
    parent_by_id = {document["parent_id"]: document for document in documents}
    evidence_by_id: dict[str, tuple[dict, dict]] = {}
    for document in documents:
        _register_document(document, evidence_by_id)
    _require_supersession_targets(documents, parent_by_id)
    for query in queries:
        _require_query(query, parent_by_id, evidence_by_id, corpus)
    _require_language_coverage(queries)
    return corpus


def build_candidates(corpus: dict) -> list[Candidate]:
    candidates = []
    for document in corpus["documents"]:
        for span in document["evidence_spans"]:
            candidates.append(
                Candidate(
                    evidence_id=span["evidence_id"],
                    parent_id=document["parent_id"],
                    relative_path=document["relative_path"],
                    language=document["language"],
                    project=document["project"],
                    status=document["status"],
                    valid_from=document["valid_from"],
                    valid_to=document["valid_to"],
                    heading_path=tuple(span["heading_path"]),
                    text=span["text"],
                )
            )
    return sorted(candidates, key=lambda candidate: candidate.evidence_id)


def _in_temporal_scope(candidate: Candidate, scope: QueryScope) -> bool:
    if scope.temporal_mode == "current":
        return candidate.status == "active"
    assert scope.as_of is not None
    if candidate.valid_from > scope.as_of:
        return False
    return candidate.valid_to is None or scope.as_of < candidate.valid_to


def filter_candidates(candidates: Sequence[Candidate], scope: QueryScope) -> list[Candidate]:
    return [
        candidate
        for candidate in candidates
        if candidate.project in scope.projects and _in_temporal_scope(candidate, scope)
    ]
