"""The retrieval v2 corpus is canonical, balanced and verified by its loader before any run."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, canonical_json_bytes, validate_schema

from tests.retrieval_v2_helpers import CORPUS, SCHEMA

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_contract  # noqa: E402
import retrieval_v2_corpus  # noqa: E402
import retrieval_v2_metrics  # noqa: E402


def _assert_closed_object(rule: dict) -> None:
    if rule.get("type") != "object":
        return
    assert rule.get("additionalProperties") is False
    assert set(rule.get("required", ())) == set(rule.get("properties", ()))


def _visit_schema(rule: object) -> None:
    """Every object level of the schema must be closed and fully required."""
    if isinstance(rule, dict):
        _assert_closed_object(rule)
        for value in rule.values():
            _visit_schema(value)
        return
    if isinstance(rule, list):
        for value in rule:
            _visit_schema(value)


def _assert_both_answerabilities(queries, language: str) -> None:
    language_queries = [query for query in queries if query["language"] == language]
    assert {query["answerability"] for query in language_queries} == {
        "answerable",
        "unanswerable",
    }


def test_schema_is_closed_at_every_object_level():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    _visit_schema(schema)
    assert "$ref" not in SCHEMA.read_text(encoding="utf-8")


def test_corpus_is_canonical_closed_and_semantically_valid():
    raw = CORPUS.read_bytes()
    corpus = json.loads(raw)

    validate_schema(corpus, SCHEMA)
    assert canonical_json_bytes(corpus) + b"\n" == raw
    assert retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA) == corpus


def test_corpus_has_balanced_public_synthetic_coverage():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    queries = corpus["queries"]
    _assert_language_coverage(queries)
    required_types = {
        "exact-name",
        "relative-path",
        "command",
        "code-symbol",
        "paraphrase",
        "cross-language",
        "temporal-decision",
        "supersession",
        "contradiction",
        "multi-parent-synthesis",
        "no-answer",
        "prompt-injection",
    }
    assert required_types <= {query["query_type"] for query in queries}
    assert corpus["description"].lower().startswith("public synthetic")
    assert _all_synthetic(corpus["documents"])


def test_loader_verifies_offsets_hashes_references_and_privacy(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    document = corpus["documents"][0]
    span = document["evidence_spans"][0]
    encoded = document["parent_text"].encode("utf-8")
    assert encoded[span["byte_start"] : span["byte_end"]].decode("utf-8") == span["text"]

    bad_cases = []
    bad_hash = json.loads(json.dumps(corpus))
    bad_hash["documents"][0]["evidence_spans"][0]["span_sha256"] = "0" * 64
    bad_cases.append(bad_hash)
    bad_ref = json.loads(json.dumps(corpus))
    bad_ref["queries"][0]["required_evidence_spans"][0] = "missing-evidence"
    bad_cases.append(bad_ref)
    private_path = json.loads(json.dumps(corpus))
    private_path["documents"][0]["relative_path"] = "C:/Users/Alice/private.md"
    bad_cases.append(private_path)
    duplicate_id = json.loads(json.dumps(corpus))
    duplicate_id["documents"][1]["parent_id"] = duplicate_id["documents"][0]["parent_id"].upper()
    bad_cases.append(duplicate_id)
    overlap = json.loads(json.dumps(corpus))
    first_span = overlap["documents"][0]["evidence_spans"][0]
    overlap["documents"][0]["evidence_spans"].append(dict(first_span, evidence_id="overlap"))
    bad_cases.append(overlap)
    query_leak = json.loads(json.dumps(corpus))
    query_leak["queries"][0]["answer_text"] = "gold answer"
    bad_cases.append(query_leak)

    for index, bad in enumerate(bad_cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(canonical_json_bytes(bad) + b"\n")
        with pytest.raises((ValueError, SchemaValidationError)):
            retrieval_v2_corpus.load_corpus(path, SCHEMA)


def test_loader_rejects_answerability_temporal_and_project_inconsistency(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(corpus))
    query = next(item for item in bad["queries"] if item["answerability"] == "answerable")
    query["relevant_parents"] = []
    path = tmp_path / "bad-answerability.json"
    path.write_bytes(canonical_json_bytes(bad) + b"\n")
    with pytest.raises((ValueError, SchemaValidationError)):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)

    bad = json.loads(json.dumps(corpus))
    query = next(item for item in bad["queries"] if item["answerability"] == "answerable")
    query["project_scope"] = ["unrelated-project"]
    path.write_bytes(canonical_json_bytes(bad) + b"\n")
    with pytest.raises(ValueError, match="project"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)


def test_loader_rejects_extra_relevant_parent_without_required_evidence(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if item["answerability"] == "answerable")
    extra = next(
        document["parent_id"]
        for document in corpus["documents"]
        if document["parent_id"] not in query["relevant_parents"]
    )
    query["relevant_parents"].append(extra)
    path = tmp_path / "extra-relevant-parent.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match="exactly match required evidence parents"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)


def test_loader_rejects_missing_relevant_parent_for_required_evidence(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if len(item["relevant_parents"]) > 1)
    query["relevant_parents"].pop()
    path = tmp_path / "missing-relevant-parent.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match="exactly match required evidence parents"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)


def test_loader_rejects_invalid_dates_intervals_and_unscoped_negatives(tmp_path):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    invalid_date = json.loads(json.dumps(corpus))
    invalid_date["documents"][0]["valid_from"] = "2025-02-31"
    path = tmp_path / "invalid-date.json"
    path.write_bytes(canonical_json_bytes(invalid_date) + b"\n")
    with pytest.raises(ValueError, match="date"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)

    reversed_interval = json.loads(json.dumps(corpus))
    superseded = next(item for item in reversed_interval["documents"] if item["valid_to"])
    superseded["valid_to"] = date.fromisoformat(superseded["valid_from"]).isoformat()
    path = tmp_path / "reversed-interval.json"
    path.write_bytes(canonical_json_bytes(reversed_interval) + b"\n")
    with pytest.raises(ValueError, match="interval"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)

    no_eligible_negative = json.loads(json.dumps(corpus))
    query = next(item for item in no_eligible_negative["queries"] if item["query_id"] == "q-en-temporal")
    query["negative_candidates"] = ["aurora-rollback-current"]
    path = tmp_path / "unscoped-negative.json"
    path.write_bytes(canonical_json_bytes(no_eligible_negative) + b"\n")
    with pytest.raises(ValueError, match="eligible negative"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("relevant_parents", None),
        ("required_evidence_spans", None),
        ("negative_candidates", None),
        ("graded_evidence", {"gain": 1}),
        ("graded_evidence", {"gain": 3}),
    ],
)
def test_loader_rejects_duplicate_query_candidate_ids(tmp_path, field, duplicate):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    query = next(item for item in corpus["queries"] if item["answerability"] == "answerable")
    if field == "graded_evidence":
        item = dict(query[field][0])
        item.update(duplicate)
    else:
        item = query[field][0]
    query[field].append(item)
    path = tmp_path / f"duplicate-{field}.json"
    path.write_bytes(canonical_json_bytes(corpus) + b"\n")

    with pytest.raises(ValueError, match=f"duplicate normalized {field.replace('_', ' ')}"):
        retrieval_v2_corpus.load_corpus(path, SCHEMA)


def _assert_language_coverage(queries) -> None:
    """Each language carries enough queries, and cross-language ones exist."""
    languages = Counter(query["language"] for query in queries)
    for language in ("EN", "RU", "ZH"):
        assert languages[language] >= 6
        _assert_both_answerabilities(queries, language)
    assert sum(query["cross_language"] for query in queries) >= 3


def _all_synthetic(documents) -> bool:
    return all(
        document["relative_path"].startswith("synthetic/") for document in documents
    )


def _all_have(candidates, attribute: str, expected) -> bool:
    return all(getattr(candidate, attribute) == expected for candidate in candidates)


def _assert_negatives_survive_filters(candidates, query) -> None:
    """Hard filters must keep the negatives the query is scored against."""
    scoped = retrieval_v2_corpus.filter_candidates(candidates, retrieval_v2_metrics.query_scope(query))
    assert set(query["negative_candidates"]) & {
        candidate.evidence_id for candidate in scoped
    }


def _evidence_languages(corpus) -> dict[str, str]:
    return {
        span["evidence_id"]: document["language"]
        for document in corpus["documents"]
        for span in document["evidence_spans"]
    }


def _assert_requires_language_transfer(query, evidence_languages) -> None:
    """A cross-language query answers in another language and names no files."""
    for evidence_id in query["required_evidence_spans"]:
        assert evidence_languages[evidence_id] != query["language"]
    assert not re.search(r"\b[\w-]+\.(?:py|sqlite3|md)\b", query["text"])


def test_filters_apply_before_scoring_and_keep_eligible_negatives():
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    candidates = retrieval_v2_corpus.build_candidates(corpus)
    current = retrieval_v2_corpus.filter_candidates(
        candidates,
        retrieval_v2_contract.QueryScope(projects=("aurora",), temporal_mode="current", as_of=None),
    )
    historical = retrieval_v2_corpus.filter_candidates(
        candidates,
        retrieval_v2_contract.QueryScope(projects=("aurora",), temporal_mode="as_of", as_of="2025-03-01"),
    )
    assert current
    assert _all_have(current, "project", "aurora")
    assert _all_have(current, "status", "active")
    assert any(candidate.status == "superseded" for candidate in historical)
    for query in corpus["queries"]:
        _assert_negatives_survive_filters(candidates, query)


def test_cross_language_queries_require_language_transfer_not_filename_overlap():
    corpus = retrieval_v2_corpus.load_corpus(CORPUS, SCHEMA)
    evidence_languages = _evidence_languages(corpus)
    cross_language = [query for query in corpus["queries"] if query["cross_language"]]

    assert len(cross_language) >= 3
    for query in cross_language:
        _assert_requires_language_transfer(query, evidence_languages)


def test_loader_rejects_oversized_input_before_json_decode(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (retrieval_v2_contract.MAX_CORPUS_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        retrieval_v2_corpus.load_corpus(oversized, SCHEMA)


def test_loader_rejects_oversized_schema_before_validation(tmp_path):
    oversized = tmp_path / "oversized-schema.json"
    oversized.write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(ValueError, match="retrieval schema exceeds"):
        retrieval_v2_corpus.load_corpus(CORPUS, oversized)
