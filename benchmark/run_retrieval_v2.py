"""Run the frozen public synthetic retrieval-v2 benchmark.

The effectiveness metrics follow the TREC definitions documented by
``ir-measures``: Recall@k is the fraction of all known relevant candidates
retrieved (not Success@k), MRR@10 is the reciprocal rank of the first relevant
candidate, and nDCG@10 uses graded gains with log2 discount normalized against
the ideal ranking. See https://ir-measur.es/en/latest/measures.html.

The adapters in this module are deterministic test machinery. Their fixed
multilingual aliases and hash vectors validate orchestration only; every report
therefore says ``quality_claim: false`` and
``adapter_kind: deterministic-fake``. They are not model-quality evidence.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path, PurePosixPath

from packaging.markers import Marker

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from bounded_io import read_stable_bytes  # noqa: E402
from reliable_memory import (  # noqa: E402
    SchemaValidationError,
    canonical_json_bytes,
    validate_schema,
)

# Explicit sentinel: Task 8 adapters have no network implementation or model download path.
urlopen = None


DEFAULT_CORPUS = Path(__file__).with_name("retrieval-v2.json")
DEFAULT_SCHEMA = Path(__file__).with_name("retrieval-v2.schema.json")
DEFAULT_MATRIX = Path(__file__).with_name("model-matrix-v1.json")
ADAPTER_KIND = "deterministic-fake"
MODEL_MATRIX_ADAPTER_KIND = "model-matrix"
SELECTION_AGGREGATION_ADAPTER_KIND = "selection-aggregation"
AUTHORITATIVE_SELECTION_ADAPTER_KIND = "authoritative-selection"
MAX_CANDIDATES = 50
MAX_CORPUS_BYTES = 2 * 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
DEFAULT_LEXICAL_DEADLINE_SECONDS = 30.0
MAX_SEGMENTATION_INPUT_CHARS = 100_000
MAX_SEGMENTATION_TOKENS = 10_000
JIEBA_VERSION = "0.42.1"
# SHA256 of jieba-0.42.1/jieba/dict.txt from the locked PyPI artifact.
JIEBA_DEFAULT_DICTIONARY_SHA256 = (
    "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
)
BGE_M3_SPARSE_LINEAR_SHA256 = (
    "45c93804d2142b8f6d7ec6914ae23a1eee9c6a1d27d83d908a20d2afb3595ad9"
)
THRESHOLDS = {
    "parent_recall_at_10": 0.95,
    "all_required_evidence_recall_at_20": 0.85,
    "ndcg_at_10": 0.80,
    "mrr_at_10": 0.85,
    "max_language_gate_gap": 0.03,
    "no_answer_false_answer_rate": 0.03,
}
EFFECTIVENESS_FIELDS = (
    "evidence_recall_at_10",
    "evidence_recall_at_20",
    "evidence_recall_at_50",
    "all_required_evidence_recall_at_20",
    "parent_recall_at_10",
    "ndcg_at_10",
    "mrr_at_10",
    "no_answer_false_answer_rate",
)
REPORT_FIELDS = {
    "schema_version",
    "corpus_id",
    "adapter_kind",
    "quality_claim",
    "methodology",
    "overall",
    "slices",
    "macro_average",
    "thresholds",
    "gates",
    "measurements",
    "traces",
    "model_id",
    "variant_id",
    "revision",
    "matrix_sha256",
    "corpus_sha256",
    "acquisition_mode",
    "vector_backend",
    "reranker",
    "release_evidence",
    "requested_mode",
    "effective_mode",
    "fallback_reason",
    "benchmark_contract_sha256",
    "benchmark_runner_sha256",
    "candidate",
}
SELECTION_ARTIFACT_FIELDS = {
    "baseline",
    "benchmark_contract_sha256",
    "benchmark_runner_sha256",
    "candidate_reports",
    "corpus_sha256",
    "gates",
    "matrix_policy_sha256",
    "matrix_sha256",
    "measured_at",
    "measurements",
    "pareto",
    "quality_claim",
    "release_evidence",
    "schema_version",
    "selected",
}
LEXICAL_CONFIGURATIONS = {
    "L0": {
        "id": "L0",
        "indexes": {"unicode": "unicode61 remove_diacritics 2"},
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": None,
    },
    "L1": {
        "id": "L1",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "english_porter": "porter unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": None,
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L2": {
        "id": "L2",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "chinese_trigram": "trigram",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": "unicode",
        "segmentation": None,
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L3": {
        "id": "L3",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "chinese_jieba": "unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": None,
        "segmentation": {"dependency": "jieba", "version": "0.42.1", "HMM": False},
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
    "L4": {
        "id": "L4",
        "indexes": {
            "unicode": "unicode61 remove_diacritics 2",
            "english_porter": "porter unicode61 remove_diacritics 2",
            "chinese_trigram": "trigram",
            "chinese_jieba": "unicode61 remove_diacritics 2",
        },
        "routing": {"strategy": "all-indexes", "candidate_universe": "all-eligible"},
        "fallback": "unicode",
        "segmentation": {"dependency": "jieba", "version": "0.42.1", "HMM": False},
        "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
    },
}


@dataclass(frozen=True)
class QueryScope:
    projects: tuple[str, ...]
    temporal_mode: str
    as_of: str | None


@dataclass(frozen=True)
class Candidate:
    evidence_id: str
    parent_id: str
    relative_path: str
    language: str
    project: str
    status: str
    valid_from: str
    valid_to: str | None
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    score: float

    @property
    def evidence_id(self) -> str:
        return self.candidate.evidence_id

    @property
    def parent_id(self) -> str:
        return self.candidate.parent_id

    @property
    def text(self) -> str:
        return self.candidate.text


@dataclass(frozen=True)
class ModelSelection:
    matrix: dict
    embedding: dict
    variant: dict
    reranker: dict | None
    reranker_variant: dict | None
    matrix_sha256: str
    corpus_sha256: str


@dataclass(frozen=True)
class _WorkerPayload:
    report: dict
    canonical_bytes: bytes


def _require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} is not a closed canonical matrix object")


def _validate_matrix_candidate(candidate: dict, *, kind: str, contract: dict) -> None:
    fields = {
        "architecture",
        "batch_size",
        "benchmark_max_tokens",
        "cache_policy",
        "exclusion_reasons",
        "formatting",
        "id",
        "kind",
        "languages",
        "license",
        "native_library",
        "native_max_tokens",
        "revision",
        "shipping_eligible",
        "source_url",
        "trust_remote_code",
        "variants",
    }
    if kind == "embedding":
        fields.add("inference")
    _require_exact_keys(candidate, fields, f"{kind} candidate")
    if candidate["kind"] != kind:
        raise ValueError(f"matrix {kind} kind mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate["revision"]):
        raise ValueError(f"matrix {kind} revision is not pinned")
    if candidate["trust_remote_code"] is not False:
        raise ValueError(f"matrix {kind} requires remote code")
    if candidate["cache_policy"] != contract["cache_policy"]:
        raise ValueError(f"matrix {kind} cache policy mismatch")
    if candidate["batch_size"] != contract["batch_size"]:
        raise ValueError(f"matrix {kind} batch size mismatch")
    if candidate["license"] not in {"Apache-2.0", "Gemma", "MIT"}:
        raise ValueError(f"matrix {kind} license is unknown")
    if candidate["shipping_eligible"]:
        if candidate["license"] not in {"Apache-2.0", "MIT"} or candidate["exclusion_reasons"]:
            raise ValueError(f"matrix {kind} shipping policy is inconsistent")
    elif not candidate["exclusion_reasons"]:
        raise ValueError(f"matrix {kind} exclusion lacks a reason")
    if not candidate["languages"] or not set(candidate["languages"]) <= set(contract["languages"]):
        raise ValueError(f"matrix {kind} language coverage is invalid")
    _require_exact_keys(candidate["native_library"], {"name", "support"}, "native library")
    if (
        candidate["native_library"]["name"] not in {"sentence-transformers", "transformers"}
        or candidate["native_library"]["support"] != "native"
    ):
        raise ValueError(f"matrix {kind} lacks native library support")
    if candidate["source_url"] != (
        f"https://huggingface.co/{candidate['id']}/tree/{candidate['revision']}"
    ):
        raise ValueError(f"matrix {kind} source is not revision pinned")
    if not candidate["variants"]:
        raise ValueError(f"matrix {kind} has no explicit variants")
    if kind == "embedding":
        _require_exact_keys(
            candidate["formatting"], {"document", "instruction", "query"}, "embedding formatting"
        )
        _require_exact_keys(
            candidate["inference"],
            {"l2_normalize", "max_length_tokens", "padding_side", "pooling", "truncation_side"},
            "embedding inference",
        )
        if not candidate["formatting"]["query"] or not candidate["formatting"]["document"]:
            raise ValueError("embedding candidate lacks explicit formatting defaults")
    else:
        _require_exact_keys(
            candidate["formatting"],
            {
                "assistant_suffix",
                "contract_type",
                "document_template",
                "instruction",
                "max_length_tokens",
                "query_template",
                "score_tokens",
                "scoring",
                "system_prefix",
                "truncation",
                "user_template",
            },
            "reranker formatting",
        )
    for variant in candidate["variants"]:
        expected = {
            "dimensions",
            "precision",
            "quality",
            "resource_measurements",
            "variant_id",
        }
        if kind == "embedding":
            expected.add("mrl")
        _require_exact_keys(variant, expected, f"{kind} variant")
        _require_exact_keys(
            variant["quality"], {"claim", "overall", "per_language", "status"}, "variant quality"
        )
        _require_exact_keys(
            variant["quality"]["per_language"], {"EN", "RU", "ZH"}, "variant languages"
        )
        _require_exact_keys(
            variant["resource_measurements"],
            {
                "cold_first_query_ms",
                "index_bytes",
                "indexing_throughput_documents_per_second",
                "model_load_ms",
                "peak_rss_bytes",
                "status",
                "vector_bytes_per_document",
                "warm_p50_ms",
                "warm_p95_ms",
            },
            "variant resource measurements",
        )
        if variant["precision"] != contract["precision"]:
            raise ValueError(f"matrix {kind} variant precision mismatch")
        if kind == "embedding":
            _require_exact_keys(
                variant["mrl"],
                {"enabled", "renormalize_after_truncation", "truncate_to_dimensions"},
                "embedding MRL",
            )


def load_model_selection(
    matrix_path: Path | str,
    corpus_path: Path | str,
    *,
    model_id: str,
    variant_id: str,
    reranker_id: str | None = None,
) -> ModelSelection:
    matrix_path = Path(matrix_path)
    raw = read_stable_bytes(matrix_path, MAX_CORPUS_BYTES, label="model matrix")
    try:
        matrix = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot load model matrix: {exc}") from exc
    if canonical_json_bytes(matrix) + b"\n" != raw:
        raise ValueError("model matrix bytes are not canonical and frozen")
    _require_exact_keys(
        matrix,
        {"artifact_kind", "benchmark_contract", "embeddings", "lexical", "rerankers", "schema_version", "selection"},
        "model matrix",
    )
    if matrix["artifact_kind"] != "model-policy-matrix" or matrix["schema_version"] != 1:
        raise ValueError("unsupported model matrix")
    contract = matrix["benchmark_contract"]
    _require_exact_keys(
        contract,
        {
            "batch_size",
            "cache_policy",
            "corpus",
            "languages",
            "precision",
            "quality_claims_allowed",
            "reranker_depths",
        },
        "benchmark contract",
    )
    _require_exact_keys(
        contract["cache_policy"],
        {"network_access", "offline_required", "revision_scoped"},
        "benchmark cache policy",
    )
    _require_exact_keys(contract["corpus"], {"path", "sha256"}, "benchmark corpus")
    _require_exact_keys(
        matrix["selection"],
        {
            "aggregation_evidence_contract",
            "baseline",
            "default_embedding",
            "default_reranker",
            "limits",
            "material_improvement",
            "pareto_objectives",
            "predetermined_winner",
            "required_gates",
            "requires_pareto_efficient",
            "requires_raw_benchmark_result",
            "result_evidence",
            "result_evidence_contract",
            "selection_target_fields",
            "status",
        },
        "matrix selection",
    )
    aggregation_contract = matrix["selection"]["aggregation_evidence_contract"]
    _require_exact_keys(
        aggregation_contract,
        {
            "artifact_schema",
            "complete_candidate_set",
            "output_path_policy",
            "required_fields",
            "schema_version",
        },
        "aggregation evidence contract",
    )
    if set(aggregation_contract["required_fields"]) != SELECTION_ARTIFACT_FIELDS:
        raise ValueError("aggregation evidence fields do not match runner contract")
    if (
        aggregation_contract["schema_version"] != 1
        or aggregation_contract["artifact_schema"] != "retrieval-selection/v1"
        or aggregation_contract["output_path_policy"]
        != "normalized_repo_relative_json_under_benchmark_results"
    ):
        raise ValueError("unsupported aggregation evidence contract")
    _require_exact_keys(
        matrix["selection"]["baseline"],
        {
            "overall_basis_points",
            "parent_recall_at_10_basis_points",
            "policy_sha256",
            "raw_report_path",
            "raw_report_sha256",
        },
        "selection baseline",
    )
    _require_exact_keys(
        matrix["selection"]["limits"],
        {"peak_rss_bytes", "warm_p95_ms"},
        "selection limits",
    )
    _require_exact_keys(
        matrix["selection"]["material_improvement"],
        {"metric", "minimum_absolute_gain_basis_points"},
        "selection material improvement",
    )
    _require_exact_keys(
        matrix["lexical"],
        {
            "dictionary_sha256",
            "dictionary_sha256_status",
            "exclusion_reasons",
            "hmm",
            "id",
            "kind",
            "license",
            "package_sha256",
            "quality",
            "query_document_configuration_identical",
            "resource_measurements",
            "shipping_eligible",
            "source_url",
            "version",
        },
        "matrix lexical policy",
    )
    if contract["precision"] != "float32" or contract["batch_size"] != 8:
        raise ValueError("unsupported benchmark precision or batch size")
    if contract["quality_claims_allowed"] is not True:
        raise ValueError("matrix does not permit provenance-bound candidate quality claims")
    if contract["cache_policy"] != {
        "network_access": "prefetch_only",
        "offline_required": True,
        "revision_scoped": True,
    }:
        raise ValueError("unsupported benchmark cache policy")
    if contract["reranker_depths"] != [10, 20, 50]:
        raise ValueError("unsupported reranker depths")
    corpus_path = Path(corpus_path)
    corpus_hash = _sha256_file(corpus_path)
    if corpus_hash != contract["corpus"]["sha256"]:
        raise ValueError("benchmark corpus SHA256 does not match matrix")
    for candidate in matrix["embeddings"]:
        _validate_matrix_candidate(candidate, kind="embedding", contract=contract)
    for candidate in matrix["rerankers"]:
        _validate_matrix_candidate(candidate, kind="reranker", contract=contract)
    embedding = next((item for item in matrix["embeddings"] if item["id"] == model_id), None)
    if embedding is None:
        raise ValueError(f"unknown embedding model: {model_id}")
    variant = next((item for item in embedding["variants"] if item["variant_id"] == variant_id), None)
    if variant is None:
        raise ValueError(f"unknown embedding variant: {variant_id}")
    if variant["precision"] != "float32" or not isinstance(variant["dimensions"], int):
        raise ValueError("embedding variant is not pinned to float32 dimensions")
    reranker = None
    reranker_variant = None
    if reranker_id is not None:
        reranker = next((item for item in matrix["rerankers"] if item["id"] == reranker_id), None)
        if reranker is None:
            raise ValueError(f"unknown reranker model: {reranker_id}")
        if len(reranker["variants"]) != 1:
            raise ValueError("reranker lacks one explicit matrix variant")
        reranker_variant = reranker["variants"][0]
        if reranker_variant["precision"] != "float32":
            raise ValueError("reranker variant is not pinned to float32")
    return ModelSelection(
        matrix=matrix,
        embedding=embedding,
        variant=variant,
        reranker=reranker,
        reranker_variant=reranker_variant,
        matrix_sha256=hashlib.sha256(raw).hexdigest(),
        corpus_sha256=corpus_hash,
    )


def _matrix_target(candidate: dict, variant: dict) -> dict:
    return {
        "dimensions": variant["dimensions"],
        "id": candidate["id"],
        "revision": candidate["revision"],
        "variant_id": variant["variant_id"],
    }


def required_candidate_specs(matrix: dict) -> list[dict]:
    rerankers = [
        _matrix_target(candidate, candidate["variants"][0])
        for candidate in matrix["rerankers"]
    ]
    return [
        {"embedding": _matrix_target(candidate, variant), "reranker": reranker}
        for candidate in matrix["embeddings"]
        for variant in candidate["variants"]
        for reranker in [None, *rerankers]
    ]


def _normalized_id(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _assert_unique(values: Iterable[str], label: str) -> None:
    normalized = [_normalized_id(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"duplicate normalized {label}")


def _validate_relative_path(value: str) -> None:
    lowered = value.casefold()
    path = PurePosixPath(value)
    if (
        not value.startswith("synthetic/")
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(marker in lowered for marker in ("/users/", "/home/", "private", "personal"))
    ):
        raise ValueError(f"non-public or non-relative synthetic path: {value}")


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label} date: {value}") from exc


def load_corpus(corpus_path: Path | str, schema_path: Path | str) -> dict:
    """Load and fail closed on schema, frozen-byte, and cross-reference errors."""
    corpus_path = Path(corpus_path)
    schema_path = Path(schema_path)
    try:
        raw = read_stable_bytes(corpus_path, MAX_CORPUS_BYTES, label="retrieval corpus")
        corpus = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load corpus {corpus_path}: {exc}") from exc
    try:
        schema_raw = read_stable_bytes(schema_path, MAX_SCHEMA_BYTES, label="retrieval schema")
        json.loads(schema_raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load schema {schema_path}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-schema-") as temporary:
        bounded_schema = Path(temporary) / "schema.json"
        bounded_schema.write_bytes(schema_raw)
        validate_schema(corpus, bounded_schema)
    if canonical_json_bytes(corpus) + b"\n" != raw:
        raise ValueError("corpus bytes are not canonical and frozen")
    if corpus["schema_version"] != "retrieval-corpus/v2":
        raise ValueError("unsupported retrieval corpus schema version")
    if corpus["corpus_id"] != "public-synthetic-retrieval-v2":
        raise ValueError("unexpected frozen corpus id")

    documents = corpus["documents"]
    queries = corpus["queries"]
    if documents != sorted(documents, key=lambda item: item["parent_id"]):
        raise ValueError("documents must be frozen in parent_id order")
    if queries != sorted(queries, key=lambda item: item["query_id"]):
        raise ValueError("queries must be frozen in query_id order")
    _assert_unique((document["parent_id"] for document in documents), "parent id")
    _assert_unique((query["query_id"] for query in queries), "query id")

    parent_by_id = {document["parent_id"]: document for document in documents}
    evidence_by_id: dict[str, tuple[dict, dict]] = {}
    for document in documents:
        _validate_relative_path(document["relative_path"])
        valid_from = _parse_date(document["valid_from"], f"{document['parent_id']} valid_from")
        valid_to = (
            _parse_date(document["valid_to"], f"{document['parent_id']} valid_to")
            if document["valid_to"] is not None
            else None
        )
        if valid_to is not None and valid_from >= valid_to:
            raise ValueError(f"invalid validity interval for {document['parent_id']}")
        if document["status"] == "active":
            if document["valid_to"] is not None or document["superseded_by"] is not None:
                raise ValueError(f"active parent has supersession metadata: {document['parent_id']}")
        else:
            if document["valid_to"] is None or document["superseded_by"] is None:
                raise ValueError(f"superseded parent lacks lifecycle metadata: {document['parent_id']}")
        encoded = document["parent_text"].encode("utf-8")
        ranges: list[tuple[int, int]] = []
        for span in document["evidence_spans"]:
            evidence_id = span["evidence_id"]
            if _normalized_id(evidence_id) in {
                _normalized_id(existing) for existing in evidence_by_id
            }:
                raise ValueError(f"duplicate normalized evidence id: {evidence_id}")
            start = span["byte_start"]
            end = span["byte_end"]
            if start >= end or end > len(encoded):
                raise ValueError(f"invalid UTF-8 range for {evidence_id}")
            try:
                selected = encoded[start:end].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"range splits UTF-8 for {evidence_id}") from exc
            if selected != span["text"]:
                raise ValueError(f"UTF-8 range text mismatch for {evidence_id}")
            digest = hashlib.sha256(span["text"].encode("utf-8")).hexdigest()
            if digest != span["span_sha256"]:
                raise ValueError(f"span hash mismatch for {evidence_id}")
            if any(start < other_end and other_start < end for other_start, other_end in ranges):
                raise ValueError(f"overlapping evidence spans in {document['parent_id']}")
            ranges.append((start, end))
            evidence_by_id[evidence_id] = (document, span)

    for document in documents:
        target = document["superseded_by"]
        if target is not None:
            if target not in parent_by_id or parent_by_id[target]["status"] != "active":
                raise ValueError(f"invalid supersession target for {document['parent_id']}")

    for query in queries:
        answerable = query["answerability"] == "answerable"
        gold_fields = (
            query["relevant_parents"],
            query["required_evidence_spans"],
            query["graded_evidence"],
        )
        if answerable:
            if not all(gold_fields) or query["allowed_abstention_reason"] is not None:
                raise ValueError(f"invalid answerable gold contract: {query['query_id']}")
        elif any(gold_fields) or query["allowed_abstention_reason"] is None:
            raise ValueError(f"invalid unanswerable gold contract: {query['query_id']}")
        if query["temporal_scope"]["mode"] == "current":
            if query["temporal_scope"]["as_of"] is not None:
                raise ValueError(f"current query has as_of: {query['query_id']}")
        elif query["temporal_scope"]["as_of"] is None:
            raise ValueError(f"historical query lacks as_of: {query['query_id']}")
        else:
            _parse_date(query["temporal_scope"]["as_of"], f"{query['query_id']} as_of")

        for field in ("relevant_parents", "required_evidence_spans", "negative_candidates"):
            _assert_unique(query[field], field.replace("_", " "))
        _assert_unique(
            (item["evidence_id"] for item in query["graded_evidence"]),
            "graded evidence",
        )
        relevant_parents = set(query["relevant_parents"])
        required_evidence = set(query["required_evidence_spans"])
        graded_evidence = {item["evidence_id"] for item in query["graded_evidence"]}
        negatives = set(query["negative_candidates"])
        if not relevant_parents <= parent_by_id.keys():
            raise ValueError(f"unknown relevant parent: {query['query_id']}")
        if not required_evidence <= evidence_by_id.keys() or not graded_evidence <= evidence_by_id.keys():
            raise ValueError(f"unknown relevant evidence: {query['query_id']}")
        if not negatives <= evidence_by_id.keys():
            raise ValueError(f"unknown negative evidence: {query['query_id']}")
        if required_evidence & negatives or graded_evidence & negatives:
            raise ValueError(f"positive and negative evidence overlap: {query['query_id']}")
        if required_evidence != graded_evidence:
            raise ValueError(f"required and graded evidence differ: {query['query_id']}")
        required_parents = {
            evidence_by_id[evidence_id][0]["parent_id"] for evidence_id in required_evidence
        }
        if relevant_parents != required_parents:
            raise ValueError(
                f"relevant parents must exactly match required evidence parents: {query['query_id']}"
            )

        scope = _query_scope(query)
        scoped = {candidate.evidence_id for candidate in filter_candidates(build_candidates(corpus), scope)}
        for evidence_id in required_evidence:
            document, _span = evidence_by_id[evidence_id]
            if document["parent_id"] not in relevant_parents:
                raise ValueError(f"evidence parent is not relevant: {query['query_id']}")
            if document["project"] not in query["project_scope"]:
                raise ValueError(f"project scope excludes gold: {query['query_id']}")
            if evidence_id not in scoped:
                raise ValueError(f"temporal scope excludes gold: {query['query_id']}")
        if not negatives & scoped:
            raise ValueError(f"query lacks an eligible negative candidate: {query['query_id']}")

    language_counts = {language: 0 for language in ("EN", "RU", "ZH")}
    language_answers = {language: set() for language in language_counts}
    for query in queries:
        language_counts[query["language"]] += 1
        language_answers[query["language"]].add(query["answerability"])
    if any(count < 6 for count in language_counts.values()):
        raise ValueError("each native language requires at least six queries")
    if any(values != {"answerable", "unanswerable"} for values in language_answers.values()):
        raise ValueError("each native language requires answerable and no-answer cases")
    if sum(query["cross_language"] for query in queries) < 3:
        raise ValueError("cross-language slice requires at least three queries")
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


def filter_candidates(candidates: Sequence[Candidate], scope: QueryScope) -> list[Candidate]:
    filtered = []
    for candidate in candidates:
        if candidate.project not in scope.projects:
            continue
        if scope.temporal_mode == "current":
            if candidate.status != "active":
                continue
        else:
            assert scope.as_of is not None
            if candidate.valid_from > scope.as_of:
                continue
            if candidate.valid_to is not None and scope.as_of >= candidate.valid_to:
                continue
        filtered.append(candidate)
    return filtered


ALIASES = {
    "каталог": "catalog",
    "目录": "catalog",
    "путь": "path",
    "пути": "path",
    "路径": "path",
    "проверка": "check",
    "проверки": "check",
    "检查": "check",
    "健": "health",
    "康": "health",
    "检": "check",
    "查": "check",
    "команда": "command",
    "命令": "command",
    "命": "command",
    "令": "command",
    "очереди": "queue",
    "очередь": "queue",
    "队列": "queue",
    "доставки": "delivery",
    "доставка": "delivery",
    "交付": "delivery",
    "текущий": "current",
    "текущему": "current",
    "当前": "current",
    "решение": "decision",
    "决定": "decision",
    "удалить": "delete",
    "удаления": "delete",
    "删除": "delete",
    "кэша": "cache",
    "缓存": "cache",
}
STOPWORDS = {
    "a",
    "an",
    "is",
    "the",
    "what",
    "where",
    "which",
    "как",
    "какая",
    "какой",
    "что",
    "的",
    "哪",
    "是",
    "什",
    "么",
}


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Keep code symbols intact while splitting paths into searchable components.
    raw = re.findall(r"[a-zа-яё0-9_]+|[\u3400-\u9fff]", normalized)
    return tuple(ALIASES.get(token, token) for token in raw if token and token not in STOPWORDS)


def _candidate_text(candidate: Candidate) -> str:
    return " ".join(
        (
            candidate.parent_id,
            candidate.relative_path,
            " ".join(candidate.heading_path),
            candidate.text,
        )
    )


def _lexical_score(query_text: str, candidate: Candidate) -> float:
    query = set(_tokens(query_text))
    text = set(_tokens(_candidate_text(candidate)))
    if not query or not text:
        return 0.0
    overlap = query & text
    phrase_bonus = 0.5 if unicodedata.normalize("NFKC", query_text).casefold() in _candidate_text(candidate).casefold() else 0.0
    return len(overlap) / math.sqrt(len(query) * len(text)) + phrase_bonus


def _hash_vector(value: str, dimensions: int = 64) -> tuple[float, ...]:
    vector = [0.0] * dimensions
    for token in _tokens(value):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(component * component for component in vector))
    if norm:
        vector = [component / norm for component in vector]
    return tuple(vector)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class FakeLexicalAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        eligible = filter_candidates(candidates, scope)
        scored = [ScoredCandidate(candidate, _lexical_score(query_text, candidate)) for candidate in eligible]
        return sorted(scored, key=lambda item: (-item.score, item.evidence_id))[:limit]


def _fts5_tokens(
    value: str, tokenizer: str, *, deadline: float | None = None
) -> tuple[str, ...]:
    allowed = {
        specification
        for configuration in LEXICAL_CONFIGURATIONS.values()
        for specification in configuration["indexes"].values()
    }
    if tokenizer not in allowed:
        raise ValueError(f"unsupported FTS5 tokenizer: {tokenizer}")
    connection = sqlite3.connect(":memory:")
    try:
        if deadline is not None:
            if time.perf_counter() >= deadline:
                raise TimeoutError("lexical benchmark absolute deadline exceeded")
            connection.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
        connection.execute(f"CREATE VIRTUAL TABLE token_source USING fts5(text, tokenize='{tokenizer}')")
        connection.execute("CREATE VIRTUAL TABLE token_vocab USING fts5vocab(token_source, 'row')")
        connection.execute("INSERT INTO token_source(text) VALUES (?)", (value,))
        result = tuple(
            row[0] for row in connection.execute("SELECT term FROM token_vocab ORDER BY term")
        )
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("lexical benchmark absolute deadline exceeded")
        return result
    except sqlite3.Error as exc:
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("lexical benchmark absolute deadline exceeded") from exc
        raise ValueError(f"required SQLite FTS5 tokenizer unavailable: {tokenizer}") from exc
    finally:
        connection.close()


def _reciprocal_rank_fusion(
    rankings: dict[str, Sequence[tuple[str, float]]], *, k: int
) -> list[tuple[str, float]]:
    active_rankings = {name: ranking for name, ranking in rankings.items() if ranking}
    if not active_rankings:
        return []
    scores: dict[str, float] = {}
    for index_name in sorted(active_rankings):
        seen = set()
        for rank, (evidence_id, _raw_bm25) in enumerate(active_rankings[index_name], 1):
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (k + rank)
    theoretical_max = len(active_rankings) / (k + 1)
    scores = {evidence_id: score / theoretical_max for evidence_id, score in scores.items()}
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def matrix_policy_fingerprint(matrix: dict) -> str:
    policy = json.loads(json.dumps(matrix))
    selection = policy["selection"]
    selection.update(
        default_embedding=None,
        default_reranker=None,
        result_evidence=None,
        status="awaiting_raw_benchmark",
    )
    quality_objects = [policy["lexical"]["quality"]]
    resource_objects = [policy["lexical"]["resource_measurements"]]
    for candidate in policy["embeddings"] + policy["rerankers"]:
        for variant in candidate["variants"]:
            quality_objects.append(variant["quality"])
            resource_objects.append(variant["resource_measurements"])
    for quality in quality_objects:
        quality.update(
            claim=None,
            overall=None,
            per_language={language: None for language in ("EN", "RU", "ZH")},
            status="unmeasured",
        )
    for resources in resource_objects:
        for field in resources:
            resources[field] = "unmeasured" if field == "status" else None
    return _sha256_json(policy)


def _environment_provenance(vector_backend: str | None) -> dict:
    names = ("sentence-transformers", "transformers", "torch", "numpy", "usearch")
    versions = {}
    for name in names:
        try:
            versions[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            versions[name] = None
    if vector_backend not in {"usearch-exact", "usearch-hnsw"}:
        versions["usearch"] = None
    return {
        "packages": versions,
        "uv_lock_sha256": _sha256_file(ROOT / "uv.lock"),
        "runner_sha256": _sha256_file(Path(__file__)),
    }


def _locked_package_versions(lock_path: Path) -> dict[str, str]:
    raw = read_stable_bytes(lock_path, 16 * 1024 * 1024, label="uv lock")
    parsed = tomllib.loads(raw.decode("utf-8"))
    observed: dict[str, set[str]] = {}
    for package in parsed.get("package", []):
        markers = package.get("resolution-markers", [])
        if markers and not any(Marker(marker).evaluate() for marker in markers):
            continue
        name = package["name"].casefold().replace("_", "-")
        observed.setdefault(name, set()).add(package["version"])
    return {name: next(iter(versions)) for name, versions in observed.items() if len(versions) == 1}


def _verify_locked_environment(
    vector_backend: str,
    *,
    lexical_config: str,
    version_getter=importlib_metadata.version,
) -> dict:
    locked = _locked_package_versions(ROOT / "uv.lock")
    required = {"sentence-transformers", "transformers", "torch", "numpy"}
    if vector_backend in {"usearch-exact", "usearch-hnsw"}:
        required.add("usearch")
    if lexical_config in {"L3", "L4"}:
        required.add("jieba")
    verified = {}
    for name in sorted(required):
        if name not in locked:
            raise ValueError(f"{name} is absent from uv.lock")
        try:
            installed = version_getter(name)
        except (KeyError, importlib_metadata.PackageNotFoundError) as exc:
            raise ValueError(f"{name} locked dependency is not installed") from exc
        if installed != locked[name]:
            raise ValueError(
                f"{name} installed version {installed} does not match locked {locked[name]}"
            )
        verified[name] = installed
    return {
        "packages": verified,
        "package_map_sha256": _sha256_json(verified),
        "uv_lock_sha256": _sha256_file(ROOT / "uv.lock"),
    }


def _load_pinned_jieba(cache_root: Path) -> tuple[object, dict]:
    try:
        module = importlib.import_module("jieba")
        distribution = importlib_metadata.distribution("jieba")
    except (ImportError, importlib_metadata.PackageNotFoundError) as exc:
        raise ValueError(f"lexical configuration requires installed jieba {JIEBA_VERSION}") from exc
    if distribution.version != JIEBA_VERSION:
        raise ValueError(f"lexical configuration requires exactly jieba {JIEBA_VERSION}")
    providers = importlib_metadata.packages_distributions().get("jieba", [])
    if providers != ["jieba"]:
        raise ValueError("jieba import does not have exact distribution provenance")
    files = {str(path).replace("\\", "/") for path in distribution.files or ()}
    required_files = {"jieba/__init__.py", "jieba/dict.txt"}
    if not required_files <= files:
        raise ValueError("jieba distribution provenance lacks required package files")
    module_path = Path(getattr(module, "__file__", "")).resolve()
    expected_module = Path(distribution.locate_file("jieba/__init__.py")).resolve()
    dictionary_path = Path(distribution.locate_file("jieba/dict.txt")).resolve()
    if module_path != expected_module:
        raise ValueError("jieba import path does not match distribution provenance")
    dictionary_hash = _sha256_file(dictionary_path)
    if dictionary_hash != JIEBA_DEFAULT_DICTIONARY_SHA256:
        raise ValueError("jieba default dictionary SHA256 does not match pinned artifact")
    tokenizer_type = getattr(module, "Tokenizer", None)
    if not callable(tokenizer_type):
        raise ValueError("installed jieba package has no Tokenizer class")
    cache_dir = cache_root / "jieba"
    if cache_dir.exists() and _is_reparse_point(cache_dir):
        raise ValueError("jieba cache directory must not be a symlink or reparse point")
    cache_dir.mkdir(mode=0o700, exist_ok=True)
    if _is_reparse_point(cache_dir):
        raise ValueError("jieba cache directory must not be a symlink or reparse point")
    resolved_cache_dir = cache_dir.resolve(strict=True)
    if resolved_cache_dir.parent != cache_root or cache_root not in resolved_cache_dir.parents:
        raise ValueError("jieba cache directory escaped lexical cache root")
    cache_stat = resolved_cache_dir.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and cache_stat.st_uid != os.getuid():
        raise PermissionError("jieba cache directory is not owned by the current user")
    if os.name == "posix" and stat.S_IMODE(cache_stat.st_mode) & 0o077:
        raise PermissionError("jieba cache directory must be owner-controlled")
    cache_name = f"jieba-{JIEBA_VERSION}.cache"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cache_name):
        raise ValueError("jieba cache filename is not a bounded basename")
    cache_path = resolved_cache_dir / cache_name
    if cache_path.exists() and _is_reparse_point(cache_path):
        raise ValueError("jieba cache file must not be a symlink or reparse point")
    tokenizer = tokenizer_type()
    tokenizer.tmp_dir = str(resolved_cache_dir)
    tokenizer.cache_file = cache_name
    tokenizer.initialize()
    if not cache_path.is_file() or _is_reparse_point(cache_path):
        raise ValueError("jieba cache file was not safely created")
    return tokenizer, {
        "provenance": "pinned-installed",
        "quality_evidence": True,
        "version": JIEBA_VERSION,
        "dictionary_sha256": dictionary_hash,
        "cache_path": PurePosixPath(cache_path.as_posix()).as_posix(),
    }


class SQLiteLexicalAdapter:
    kind = "sqlite-fts5-bm25"

    def __init__(
        self,
        candidates: Sequence[Candidate],
        cache_root: Path | str,
        lexical_config: str,
        *,
        test_segmenter=None,
        deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
        deadline: float | None = None,
    ) -> None:
        if lexical_config not in LEXICAL_CONFIGURATIONS:
            raise ValueError(f"unknown lexical configuration: {lexical_config}")
        if deadline_seconds <= 0:
            raise ValueError("lexical deadline must be positive")
        self.configuration = LEXICAL_CONFIGURATIONS[lexical_config]
        normalized_ids = [_normalized_id(candidate.evidence_id) for candidate in candidates]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("duplicate normalized candidate id")
        self._candidates = {candidate.evidence_id: candidate for candidate in candidates}
        self._candidate_universe = {
            _normalized_id(candidate.evidence_id): candidate for candidate in candidates
        }
        requested_root = Path(cache_root)
        if requested_root.exists() and _is_reparse_point(requested_root):
            raise ValueError("lexical cache root must not be a symlink or reparse point")
        requested_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(requested_root):
            raise ValueError("lexical cache root must not be a symlink or reparse point")
        self._root = _validate_cache_root(requested_root).resolve(strict=True)
        self._root_identity = self._root.stat(follow_symlinks=False)
        self.path = self._root / f"lexical-{lexical_config}.sqlite3"
        self._deadline = deadline or time.perf_counter() + deadline_seconds
        self._jieba = None
        self.segmentation_runtime = None
        if self.configuration["segmentation"] is not None:
            if test_segmenter is not None:
                if not callable(getattr(test_segmenter, "cut", None)):
                    raise ValueError("test segmenter has no callable cut function")
                self._jieba = test_segmenter
                self.segmentation_runtime = {
                    "provenance": "injected-test",
                    "quality_evidence": False,
                    "version": None,
                    "dictionary_sha256": None,
                }
            else:
                self._jieba, self.segmentation_runtime = _load_pinned_jieba(self._root)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        self._descriptor = os.open(self.path, flags, 0o600)
        self._file_identity = os.fstat(self._descriptor)
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        try:
            self._check_deadline()
            self._connection = self._connect_owned_file()
            self._connection.set_progress_handler(self._progress_handler, 1000)
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._build(candidates)
            self._check_owned_identity()
        except BaseException:
            self._cleanup(remove=True)
            raise

    def _check_deadline(self) -> None:
        if time.perf_counter() >= self._deadline:
            raise TimeoutError("lexical benchmark absolute deadline exceeded")

    def _progress_handler(self) -> int:
        return int(time.perf_counter() >= self._deadline)

    def _remaining_seconds(self) -> float:
        self._check_deadline()
        return max(0.001, self._deadline - time.perf_counter())

    def _descriptor_uri(self) -> str | None:
        if os.name != "posix":
            return None
        for base in (Path("/proc/self/fd"), Path("/dev/fd")):
            descriptor_path = base / str(self._descriptor)
            if descriptor_path.exists():
                return f"{descriptor_path.as_uri()}?mode=rw"
        return None

    def _connect_owned_file(self) -> sqlite3.Connection:
        descriptor_uri = self._descriptor_uri()
        if descriptor_uri is not None:
            connection = None
            try:
                connection = sqlite3.connect(
                    descriptor_uri, uri=True, timeout=self._remaining_seconds()
                )
                connection.set_progress_handler(self._progress_handler, 1000)
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE __descriptor_probe(value)")
                connection.rollback()
                return connection
            except sqlite3.Error:
                if connection is not None:
                    connection.close()
                self._check_deadline()
                os.ftruncate(self._descriptor, 0)
                os.lseek(self._descriptor, 0, os.SEEK_SET)
        self._check_owned_identity()
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw",
            uri=True,
            timeout=self._remaining_seconds(),
        )
        try:
            self._check_owned_identity()
        except BaseException:
            connection.close()
            raise
        return connection

    def _check_owned_identity(self) -> None:
        if not _same_path_identity(self._root, self._root_identity):
            raise PermissionError("lexical cache root changed during index operation")
        if not _same_path_identity(self.path, self._file_identity):
            raise PermissionError("lexical index path changed during index operation")

    def _build(self, candidates: Sequence[Candidate]) -> None:
        assert self._connection is not None
        for index_name, tokenizer in self.configuration["indexes"].items():
            self._check_deadline()
            table = f"fts_{index_name}"
            self._connection.execute(
                f"CREATE VIRTUAL TABLE {table} USING fts5(evidence_id UNINDEXED, text, "
                f"tokenize='{tokenizer}')"
            )
            rows = []
            for candidate in candidates:
                self._check_deadline()
                rows.append((candidate.evidence_id, self._index_text(index_name, candidate)))
            self._connection.executemany(
                f"INSERT INTO {table}(evidence_id, text) VALUES (?, ?)", rows
            )
        self._connection.commit()
        self._check_deadline()

    def _segment(self, value: str) -> str:
        assert self._jieba is not None
        self._check_deadline()
        if len(value) > MAX_SEGMENTATION_INPUT_CHARS:
            raise ValueError("segmentation input limit exceeded")
        try:
            iterator = iter(self._jieba.cut(value, HMM=False))
        except Exception as exc:
            raise ValueError(f"segmentation failed: {exc}") from exc
        tokens = []
        while True:
            self._check_deadline()
            try:
                token = next(iterator)
            except StopIteration:
                break
            except Exception as exc:
                raise ValueError(f"segmentation failed: {exc}") from exc
            normalized = str(token)
            if not normalized:
                continue
            tokens.append(normalized)
            if len(tokens) > MAX_SEGMENTATION_TOKENS:
                raise ValueError("segmentation token limit exceeded")
        self._check_deadline()
        return " ".join(tokens)

    def _index_text(self, index_name: str, candidate: Candidate) -> str:
        value = _candidate_text(candidate)
        if index_name == "chinese_jieba":
            return self._segment(value)
        if index_name == "chinese_trigram":
            return unicodedata.normalize("NFKC", value).casefold()
        return value

    def _query_terms(self, index_name: str, query_text: str) -> tuple[str, ...]:
        value = self._segment(query_text) if index_name == "chinese_jieba" else query_text
        tokenizer = self.configuration["indexes"][index_name]
        return _fts5_tokens(value, tokenizer, deadline=self._deadline)

    def _rank_index(
        self,
        index_name: str,
        query_text: str,
        eligible_ids: set[str],
        *,
        limit: int,
    ) -> list[tuple[str, float]]:
        assert self._connection is not None
        self._check_deadline()
        if not eligible_ids:
            return []
        if index_name == "chinese_trigram":
            normalized = unicodedata.normalize("NFKC", query_text).casefold()
            if len(normalized) < 3:
                return []
            fts_query = f'"{normalized.replace(chr(34), chr(34) * 2)}"'
        else:
            terms = self._query_terms(index_name, query_text)
            if not terms:
                return []
            fts_query = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
            )
        table = f"fts_{index_name}"
        placeholders = ",".join("?" for _ in eligible_ids)
        parameters = [fts_query, *sorted(eligible_ids), limit]
        try:
            rows = self._connection.execute(
                f"SELECT evidence_id, bm25({table}) AS score FROM {table} "
                f"WHERE {table} MATCH ? AND evidence_id IN ({placeholders}) "
                "ORDER BY score ASC, evidence_id ASC LIMIT ?",
                parameters,
            ).fetchall()
        except sqlite3.Error as exc:
            if time.perf_counter() >= self._deadline:
                raise TimeoutError("lexical benchmark absolute deadline exceeded") from exc
            raise ValueError(f"FTS5 query failed for {index_name}") from exc
        self._check_deadline()
        return [(str(evidence_id), float(score)) for evidence_id, score in rows]

    def _active_indexes(self, query_text: str) -> tuple[str, ...]:
        indexes = list(self.configuration["indexes"])
        normalized = unicodedata.normalize("NFKC", query_text).casefold()
        if "chinese_trigram" in indexes and len(normalized) < 3:
            indexes.remove("chinese_trigram")
        return tuple(indexes)

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
        language: str,
    ) -> list[ScoredCandidate]:
        del language
        try:
            self._check_deadline()
            supplied: dict[str, Candidate] = {}
            for candidate in candidates:
                normalized = _normalized_id(candidate.evidence_id)
                if normalized in supplied:
                    raise ValueError("candidate universe mismatch: duplicate normalized id")
                supplied[normalized] = candidate
            if supplied != self._candidate_universe:
                raise ValueError("candidate universe mismatch")
            eligible_ids = {
                candidate.evidence_id for candidate in filter_candidates(candidates, scope)
            }
            rankings = {
                index_name: self._rank_index(index_name, query_text, eligible_ids, limit=limit)
                for index_name in self._active_indexes(query_text)
            }
            if self.configuration["fusion"] is not None:
                fused = _reciprocal_rank_fusion(
                    rankings, k=int(self.configuration["fusion"]["k"])
                )[:limit]
                return [
                    ScoredCandidate(self._candidates[evidence_id], score)
                    for evidence_id, score in fused
                ]
            only_ranking = next(iter(rankings.values()), [])
            return [
                ScoredCandidate(self._candidates[evidence_id], -raw_bm25)
                for evidence_id, raw_bm25 in only_ranking[:limit]
            ]
        except BaseException:
            self._cleanup(remove=True)
            raise

    def close(self) -> None:
        self._cleanup(remove=False)

    def _cleanup(self, *, remove: bool) -> None:
        if self._closed:
            return
        path_owned = _same_path_identity(self.path, self._file_identity)
        root_owned = _same_path_identity(self._root, self._root_identity)
        if self._connection is not None:
            with contextlib.suppress(sqlite3.Error):
                self._connection.set_progress_handler(None, 0)
            with contextlib.suppress(sqlite3.Error):
                self._connection.close()
            self._connection = None
        os.close(self._descriptor)
        self._closed = True
        if remove and root_owned and path_owned:
            self.path.unlink(missing_ok=True)
            for suffix in ("-journal", "-shm", "-wal"):
                (self._root / f"{self.path.name}{suffix}").unlink(missing_ok=True)


class FakeEmbeddingAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        query_vector = _hash_vector(query_text)
        eligible = filter_candidates(candidates, scope)
        scored = [
            ScoredCandidate(candidate, _dot(query_vector, _hash_vector(_candidate_text(candidate))))
            for candidate in eligible
        ]
        return sorted(scored, key=lambda item: (-item.score, item.evidence_id))[:limit]


def _prepare_embedding_vectors(raw, *, rows: int, dimensions: int, allow_truncation: bool):
    try:
        import numpy as np
    except ImportError as exc:
        raise ValueError("model-matrix adapter requires the retrieval-benchmark extra") from exc
    vectors = np.asarray(raw)
    if vectors.ndim != 2 or vectors.shape[0] != rows:
        raise ValueError("encoder returned an invalid vector matrix shape")
    if vectors.shape[1] < dimensions or (not allow_truncation and vectors.shape[1] != dimensions):
        raise ValueError("encoder returned an invalid vector dimension")
    vectors = np.ascontiguousarray(vectors[:, :dimensions], dtype=np.float32)
    if not np.isfinite(vectors).all():
        raise ValueError("encoder vectors must be finite")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("encoder vectors must have nonzero norms")
    return np.ascontiguousarray(vectors / norms, dtype=np.float32)


def _numpy_exact_vectors(vectors, query, limit: int):
    import numpy as np

    scores = vectors @ query
    order = np.lexsort((np.arange(scores.shape[0]), -scores))[:limit]
    return order, scores[order]


def _native_usearch_search(vectors, query, limit: int, exact: bool):
    try:
        from usearch.index import Index
    except ImportError as exc:
        raise ValueError("USearch backend requires the retrieval-benchmark extra") from exc
    import numpy as np

    index = Index(ndim=vectors.shape[1], metric="cos", dtype="f32")
    keys = np.arange(vectors.shape[0], dtype=np.uint64)
    index.add(keys, vectors)
    matches = index.search(query, count=limit, exact=exact)
    return np.asarray(matches.keys), 1.0 - np.asarray(matches.distances, dtype=np.float32)


def _search_vectors(
    vectors,
    query,
    candidate_ids: Sequence[str],
    limit: int,
    *,
    backend: str,
    usearch_search=None,
) -> tuple[list[str], list[float]]:
    import numpy as np

    if vectors.ndim != 2 or query.ndim != 1 or vectors.shape[1] != query.shape[0]:
        raise ValueError("vector search dimension mismatch")
    if len(candidate_ids) != vectors.shape[0]:
        raise ValueError("vector search candidate ID mismatch")
    if not np.isfinite(vectors).all() or not np.isfinite(query).all():
        raise ValueError("vector search inputs must be finite")
    reference_keys, reference_scores = _numpy_exact_vectors(vectors, query, limit)
    keys, scores = reference_keys, reference_scores
    if backend in {"usearch-exact", "usearch-hnsw"}:
        search = usearch_search or _native_usearch_search
        keys, scores = search(vectors, query, limit, backend == "usearch-exact")
        keys = np.asarray(keys)
        scores = np.asarray(scores, dtype=np.float32)
        if backend == "usearch-exact" and (
            not np.array_equal(keys, reference_keys)
            or not np.allclose(scores, reference_scores, rtol=1e-5, atol=1e-6)
        ):
            raise ValueError("USearch exact parity check failed")
    elif backend != "numpy-exact":
        raise ValueError(f"unknown vector backend: {backend}")
    if keys.ndim != 1 or scores.ndim != 1 or len(keys) != len(scores):
        raise ValueError("vector backend returned invalid results")
    if not np.isfinite(scores).all():
        raise ValueError("vector backend returned nonfinite scores")
    return [candidate_ids[int(key)] for key in keys], [float(score) for score in scores]


class ModelEmbeddingAdapter:
    kind = MODEL_MATRIX_ADAPTER_KIND

    def __init__(
        self,
        selection: ModelSelection,
        *,
        encoder,
        vector_backend: str = "numpy-exact",
        usearch_search=None,
    ) -> None:
        self.selection = selection
        self._encoder = encoder
        self.vector_backend = vector_backend
        self._usearch_search = usearch_search
        self._candidate_ids: tuple[str, ...] | None = None
        self._candidate_by_id: dict[str, Candidate] = {}
        self._query_vectors = {}
        self._query_sparse = {}
        self.document_vectors = None
        self.document_sparse = None
        self.learned_sparse_bytes = 0
        self.last_trace: dict | None = None

    def _encode_signals(self, texts: Sequence[str]):
        model = self.selection.embedding
        inference = model["inference"]
        raw = self._encoder(
            texts,
            batch_size=model["batch_size"],
            max_length=inference["max_length_tokens"],
            pooling=inference["pooling"],
            padding_side=inference["padding_side"],
            truncation_side=inference["truncation_side"],
        )
        sparse = None
        if isinstance(raw, dict):
            if set(raw) != {"dense_vecs", "lexical_weights"}:
                raise ValueError("encoder returned unknown embedding signals")
            sparse = raw["lexical_weights"]
            raw = raw["dense_vecs"]
        vectors = _prepare_embedding_vectors(
            raw,
            rows=len(texts),
            dimensions=self.selection.variant["dimensions"],
            allow_truncation=self.selection.variant["mrl"]["enabled"],
        )
        if sparse is not None:
            if model["id"] != "BAAI/bge-m3" or not isinstance(sparse, list) or len(sparse) != len(texts):
                raise ValueError("learned sparse output is not valid for this embedding")
            normalized = []
            for row in sparse:
                if not isinstance(row, dict):
                    raise ValueError("learned sparse output must contain token-weight mappings")
                values = {}
                for token, weight in row.items():
                    value = float(weight)
                    if not isinstance(token, str) or not token or not math.isfinite(value) or value <= 0:
                        raise ValueError("learned sparse token weights must be finite and positive")
                    values[token] = value
                normalized.append(values)
            sparse = normalized
        return vectors, sparse

    def _encode(self, texts: Sequence[str]):
        return self._encode_signals(texts)[0]

    def _format(self, text: str, field: str) -> str:
        formatting = self.selection.embedding["formatting"]
        return formatting[field].format(text=text, instruction=formatting["instruction"])

    def _ensure_documents(self, candidates: Sequence[Candidate]) -> None:
        ids = tuple(candidate.evidence_id for candidate in candidates)
        if self._candidate_ids is None:
            self._candidate_ids = ids
            self._candidate_by_id = {candidate.evidence_id: candidate for candidate in candidates}
            documents = [self._format(_candidate_text(candidate), "document") for candidate in candidates]
            self.document_vectors, self.document_sparse = self._encode_signals(documents)
            if self.document_sparse is not None:
                self.learned_sparse_bytes = sum(
                    len(token.encode("utf-8")) + 8
                    for row in self.document_sparse
                    for token in row
                )
        elif ids != self._candidate_ids:
            raise ValueError("dense candidate universe mismatch")

    def prepare_queries(self, query_texts: Sequence[str]) -> None:
        formatted = [self._format(text, "query") for text in query_texts]
        vectors, sparse = self._encode_signals(formatted)
        self._query_vectors.update(zip(formatted, vectors))
        if sparse is not None:
            self._query_sparse.update(zip(formatted, sparse))

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[Candidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        import numpy as np

        self._ensure_documents(candidates)
        assert self._candidate_ids is not None and self.document_vectors is not None
        formatted_query = self._format(query_text, "query")
        query_vector = self._query_vectors.get(formatted_query)
        query_sparse = self._query_sparse.get(formatted_query)
        if query_vector is None:
            vectors, sparse = self._encode_signals([formatted_query])
            query_vector = vectors[0]
            query_sparse = sparse[0] if sparse is not None else None
        eligible_ids = {item.evidence_id for item in filter_candidates(candidates, scope)}
        indexes = np.asarray(
            [index for index, evidence_id in enumerate(self._candidate_ids) if evidence_id in eligible_ids],
            dtype=np.int64,
        )
        vectors = self.document_vectors[indexes]
        ids = tuple(self._candidate_ids[int(index)] for index in indexes)
        ranked_ids, scores = _search_vectors(
            vectors,
            query_vector,
            ids,
            min(limit, len(ids)),
            backend=self.vector_backend,
            usearch_search=self._usearch_search,
        )
        dense = [
            ScoredCandidate(self._candidate_by_id[evidence_id], score)
            for evidence_id, score in zip(ranked_ids, scores)
        ]
        if self.document_sparse is None:
            return dense
        if query_sparse is None:
            raise ValueError("BGE-M3 query omitted learned sparse output")
        sparse_ranked = []
        for index, evidence_id in zip(indexes, ids):
            document = self.document_sparse[int(index)]
            score = sum(weight * document.get(token, 0.0) for token, weight in query_sparse.items())
            if score > 0:
                sparse_ranked.append(ScoredCandidate(self._candidate_by_id[evidence_id], score))
        sparse_ranked.sort(key=lambda item: (-item.score, item.evidence_id))
        sparse_ranked = sparse_ranked[:limit]
        fused = _fuse_rankings(
            (dense, sparse_ranked), list(self._candidate_by_id.values()), limit=limit
        )
        self.last_trace = {
            "dense_candidate_ids": [item.evidence_id for item in dense],
            "learned_sparse_candidate_ids": [item.evidence_id for item in sparse_ranked],
            "fusion": {"method": "reciprocal-rank-fusion", "k": 60},
        }
        return fused


class ModelRerankerAdapter:
    kind = MODEL_MATRIX_ADAPTER_KIND

    def __init__(self, selection: ModelSelection, *, scorer) -> None:
        if selection.reranker is None:
            raise ValueError("reranker selection is required")
        self.selection = selection
        self._scorer = scorer
        self.last_trace: dict | None = None

    def _inputs(self, query_text: str, candidates: Sequence[ScoredCandidate]):
        formatting = self.selection.reranker["formatting"]
        if formatting["contract_type"] == "tokenizer_pair_sequence_classification":
            query = formatting["query_template"].format(query=query_text)
            return [
                (query, formatting["document_template"].format(document=item.text))
                for item in candidates
            ]
        return [{"query": query_text, "document": item.text} for item in candidates]

    def rank_all(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
        *,
        limit: int,
    ) -> dict[int, list[ScoredCandidate]]:
        del scope
        frozen = list(candidates[:limit])
        frozen_ids = [item.evidence_id for item in frozen]
        fingerprint = hashlib.sha256(canonical_json_bytes(frozen_ids)).hexdigest()
        ranked_by_depth = {}
        depth_traces = {}
        for depth in self.selection.matrix["benchmark_contract"]["reranker_depths"]:
            prefix = frozen[:depth]
            started = time.perf_counter()
            scores = self._scorer(
                self._inputs(query_text, prefix),
                batch_size=self.selection.reranker["batch_size"],
                max_length=self.selection.reranker["formatting"]["max_length_tokens"],
                contract_type=self.selection.reranker["formatting"]["contract_type"],
                score_tokens=self.selection.reranker["formatting"]["score_tokens"],
            )
            if len(scores) != len(prefix) or any(
                not math.isfinite(float(score)) for score in scores
            ):
                raise ValueError("reranker returned invalid or nonfinite scores")
            rescored = [
                (index, ScoredCandidate(item.candidate, float(score)))
                for index, (item, score) in enumerate(zip(prefix, scores))
            ]
            rescored.sort(key=lambda pair: (-pair[1].score, pair[0]))
            ranked_by_depth[depth] = [item for _index, item in rescored] + frozen[depth:]
            depth_traces[str(depth)] = {
                "candidate_ids": frozen_ids[:depth],
                "duration_ms": (time.perf_counter() - started) * 1000,
                "fallback": None,
            }
        self.last_trace = {
            "model_id": self.selection.reranker["id"],
            "revision": self.selection.reranker["revision"],
            "variant_id": self.selection.reranker_variant["variant_id"],
            "formatting": json.loads(json.dumps(self.selection.reranker["formatting"])),
            "pre_rerank_candidate_ids": frozen_ids,
            "pre_rerank_fingerprint": fingerprint,
            "depths": depth_traces,
        }
        return ranked_by_depth


class FakeRerankerAdapter:
    kind = ADAPTER_KIND

    def rank(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
        *,
        limit: int,
    ) -> list[ScoredCandidate]:
        eligible_ids = {
            candidate.evidence_id
            for candidate in filter_candidates([item.candidate for item in candidates], scope)
        }
        by_id: dict[str, ScoredCandidate] = {}
        for item in candidates:
            if item.evidence_id not in eligible_ids:
                continue
            lexical = _lexical_score(query_text, item.candidate)
            score = max(item.score, 0.0) + 2.0 * lexical
            current = by_id.get(item.evidence_id)
            if current is None or score > current.score:
                by_id[item.evidence_id] = ScoredCandidate(item.candidate, score)
        return sorted(by_id.values(), key=lambda item: (-item.score, item.evidence_id))[:limit]


class FakeQAAdapter:
    kind = ADAPTER_KIND

    def answer(
        self,
        query_text: str,
        scope: QueryScope,
        candidates: Sequence[ScoredCandidate],
    ) -> dict:
        del query_text, scope
        if not candidates or candidates[0].score < 0.54:
            return {"abstained": True, "reason": "not-in-corpus"}
        return {"abstained": False, "reason": None}


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def all_required_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    return float(relevant_ids <= set(ranked_ids[:k]))


def parent_recall_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    return recall_at_k(ranked_ids, relevant_ids, k)


def reciprocal_rank_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    for rank, candidate_id in enumerate(ranked_ids[:k], 1):
        if candidate_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked_ids: Sequence[str], gains: dict[str, int], k: int) -> float:
    if not gains:
        return 0.0

    def dcg(values: Sequence[int]) -> float:
        return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(values, 1))

    actual = dcg([gains.get(candidate_id, 0) for candidate_id in ranked_ids[:k]])
    ideal = dcg(sorted(gains.values(), reverse=True)[:k])
    return actual / ideal if ideal else 0.0


def false_answer_rate(traces: Sequence[dict]) -> float | None:
    no_answer = [trace for trace in traces if trace["answerability"] == "unanswerable"]
    if not no_answer:
        return None
    return sum(not trace["abstained"] for trace in no_answer) / len(no_answer)


def macro_average(slices: dict[str, dict], metric: str) -> float | None:
    values = [
        slices[name][metric]
        for name in ("EN", "RU", "ZH", "cross-language")
        if slices[name][metric] is not None
    ]
    return sum(values) / len(values) if values else None


def language_gate_gaps(slices: dict[str, dict], metric: str, gate: float) -> dict[str, float]:
    return {language: slices[language][metric] - gate for language in ("EN", "RU", "ZH")}


def _query_scope(query: dict) -> QueryScope:
    return QueryScope(
        projects=tuple(query["project_scope"]),
        temporal_mode=query["temporal_scope"]["mode"],
        as_of=query["temporal_scope"]["as_of"],
    )


def _expand_parents(ranked: Sequence[ScoredCandidate]) -> list[str]:
    parents = []
    seen = set()
    for item in ranked:
        if item.parent_id not in seen:
            seen.add(item.parent_id)
            parents.append(item.parent_id)
    return parents


def _evaluation_row(query: dict, ranked: Sequence[ScoredCandidate], answer: dict) -> dict:
    ranked_evidence = [item.evidence_id for item in ranked]
    ranked_parents = _expand_parents(ranked)
    relevant_evidence = set(query["required_evidence_spans"])
    relevant_parents = set(query["relevant_parents"])
    gains = {item["evidence_id"]: item["gain"] for item in query["graded_evidence"]}
    return {
        "query_id": query["query_id"],
        "language": query["language"],
        "cross_language": query["cross_language"],
        "answerability": query["answerability"],
        "abstained": answer["abstained"],
        "abstention_contract_valid": (
            not answer["abstained"]
            if query["answerability"] == "answerable"
            else answer["abstained"] and answer["reason"] == query["allowed_abstention_reason"]
        ),
        "evidence_recall_at_10": recall_at_k(ranked_evidence, relevant_evidence, 10),
        "evidence_recall_at_20": recall_at_k(ranked_evidence, relevant_evidence, 20),
        "evidence_recall_at_50": recall_at_k(ranked_evidence, relevant_evidence, 50),
        "all_required_evidence_recall_at_20": all_required_at_k(
            ranked_evidence, relevant_evidence, 20
        ),
        "parent_recall_at_10": parent_recall_at_k(ranked_parents, relevant_parents, 10),
        "ndcg_at_10": ndcg_at_k(ranked_evidence, gains, 10),
        "mrr_at_10": reciprocal_rank_at_k(ranked_evidence, relevant_evidence, 10),
    }


def _aggregate(rows: Sequence[dict]) -> dict[str, float | int | None]:
    positive = [row for row in rows if row["answerability"] == "answerable"]

    def mean(name: str) -> float | None:
        return sum(row[name] for row in positive) / len(positive) if positive else None

    return {
        "query_count": len(rows),
        "positive_query_count": len(positive),
        "no_answer_query_count": len(rows) - len(positive),
        "evidence_recall_at_10": mean("evidence_recall_at_10"),
        "evidence_recall_at_20": mean("evidence_recall_at_20"),
        "evidence_recall_at_50": mean("evidence_recall_at_50"),
        "all_required_evidence_recall_at_20": mean("all_required_evidence_recall_at_20"),
        "parent_recall_at_10": mean("parent_recall_at_10"),
        "ndcg_at_10": mean("ndcg_at_10"),
        "mrr_at_10": mean("mrr_at_10"),
        "no_answer_false_answer_rate": false_answer_rate(rows),
    }


def _validate_cache_root(cache_root: Path) -> Path:
    resolved = cache_root.expanduser().resolve()
    forbidden = [ROOT.resolve()]
    configured_vault = os.environ.get("LLM_WIKI_ROOT")
    if configured_vault:
        forbidden.append(Path(configured_vault).expanduser().resolve())
    for root in forbidden:
        if resolved == root or root in resolved.parents:
            raise ValueError(f"cache root must be outside source and vault roots: {resolved}")
    return resolved


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.stat(follow_symlinks=False)
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & 0x400)


@contextlib.contextmanager
def model_cache_environment(cache_root: Path | str, *, allow_download: bool):
    requested = Path(cache_root).expanduser()
    if requested.exists() and _is_reparse_point(requested):
        raise ValueError("model cache root must not be a symlink or reparse point")
    requested.mkdir(parents=True, exist_ok=True)
    root = _validate_cache_root(requested).resolve(strict=True)
    if _is_reparse_point(root):
        raise ValueError("model cache root must not be a symlink or reparse point")
    paths = {
        "HF_HOME": root / "huggingface",
        "HF_HUB_CACHE": root / "huggingface" / "hub",
        "TRANSFORMERS_CACHE": root / "transformers",
        "SENTENCE_TRANSFORMERS_HOME": root / "sentence-transformers",
        "TORCH_HOME": root / "torch",
        "XDG_CACHE_HOME": root / "xdg",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(path) or root not in path.resolve(strict=True).parents:
            raise ValueError("model cache path escaped isolated cache root")
    updates = {name: str(path) for name, path in paths.items()}
    updates.update(
        HF_HUB_OFFLINE="0" if allow_download else "1",
        TRANSFORMERS_OFFLINE="0" if allow_download else "1",
    )
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield (
            "download-allowed-explicit-cache"
            if allow_download
            else "offline-local-files-only"
        )
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _use_posix_dir_fd() -> bool:
    return os.name == "posix"


def _same_path_identity(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return os.path.samestat(expected, current)


def _write_fake_index(corpus: dict, cache_root: Path) -> int:
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root = cache_root.resolve(strict=True)
    root_identity = cache_root.stat(follow_symlinks=False)
    payload = [
        {
            "evidence_id": candidate.evidence_id,
            "parent_id": candidate.parent_id,
            "tokens": list(_tokens(_candidate_text(candidate))),
            "vector": list(_hash_vector(_candidate_text(candidate))),
        }
        for candidate in build_candidates(corpus)
    ]
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    target_name = "fake-index.json"
    if _use_posix_dir_fd():
        directory_fd = os.open(
            cache_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        temporary_name = f".fake-index-{uuid.uuid4().hex}"
        try:
            if not os.path.samestat(root_identity, os.fstat(directory_fd)):
                raise PermissionError("cache root changed during index build")
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if not _same_path_identity(cache_root, os.fstat(directory_fd)):
                raise PermissionError("cache root changed during index build")
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            if not _same_path_identity(cache_root, os.fstat(directory_fd)):
                raise PermissionError("cache root changed during index publication")
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=directory_fd)
            os.close(directory_fd)
        return len(data)

    path = cache_root / target_name
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fake-index-", dir=cache_root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not _same_path_identity(cache_root, root_identity):
            raise PermissionError("cache root changed during index build")
        os.replace(temporary, path)
        if not _same_path_identity(cache_root, root_identity):
            raise PermissionError("cache root changed during index publication")
    finally:
        temporary.unlink(missing_ok=True)
    return len(data)


def _write_new_bytes(output: Path, data: bytes) -> os.stat_result:
    parent_identity = output.parent.stat(follow_symlinks=False)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    directory_fd = None
    opened_identity = None
    if _use_posix_dir_fd():
        directory_fd = os.open(
            output.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        if not os.path.samestat(parent_identity, os.fstat(directory_fd)):
            os.close(directory_fd)
            raise PermissionError("output parent changed before publication")
    try:
        descriptor = os.open(
            output.name if directory_fd is not None else output,
            flags,
            0o600,
            **({"dir_fd": directory_fd} if directory_fd is not None else {}),
        )
        with os.fdopen(descriptor, "wb") as handle:
            opened_identity = os.fstat(handle.fileno())
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            published_identity = (
                os.stat(output.name, dir_fd=directory_fd, follow_symlinks=False)
                if directory_fd is not None
                else output.stat(follow_symlinks=False)
            )
            if not os.path.samestat(opened_identity, published_identity):
                raise PermissionError("output changed during publication")
        expected_parent = os.fstat(directory_fd) if directory_fd is not None else parent_identity
        if not _same_path_identity(output.parent, expected_parent):
            raise PermissionError("output parent changed during publication")
        if directory_fd is not None:
            os.fsync(directory_fd)
        return opened_identity
    except BaseException:
        if opened_identity is not None:
            with contextlib.suppress(FileNotFoundError):
                published_identity = (
                    os.stat(output.name, dir_fd=directory_fd, follow_symlinks=False)
                    if directory_fd is not None
                    else output.stat(follow_symlinks=False)
                )
                if os.path.samestat(opened_identity, published_identity):
                    if directory_fd is not None:
                        os.unlink(output.name, dir_fd=directory_fd)
                    else:
                        output.unlink()
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _write_new_output(output: Path, serialized: str) -> None:
    _write_new_bytes(output, serialized.encode("utf-8"))


def _canonical_report_bytes(report: dict) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _run_process_tree(
    command: Sequence[str], *, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("worker deadline must be positive and finite")
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(list(command), **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
        else:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.kill()
            process.communicate(timeout=5)
        raise TimeoutError("real benchmark worker deadline exceeded") from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _peak_rss() -> tuple[int | None, str]:
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            if ok:
                return int(counters.PeakWorkingSetSize), "measured-windows-working-set"
        except (AttributeError, OSError, ValueError):
            pass
    else:
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            multiplier = 1 if sys.platform == "darwin" else 1024
            return int(usage * multiplier), "measured-posix-ru-maxrss"
        except (ImportError, OSError, ValueError):
            pass
    return None, "unavailable"


def _resource_measurements(
    latencies_ms: Sequence[float],
    *,
    build_time_ms: float,
    chunk_count: int,
    index_size_bytes: int,
    peak_rss_bytes: int | None,
    peak_rss_status: str,
    indexing_duration_ms: float | None = None,
) -> dict:
    sorted_latencies = sorted(latencies_ms)
    warm_latencies = sorted(latencies_ms[1:])

    def p95(values: Sequence[float]) -> float | None:
        if not values:
            return None
        return values[max(0, math.ceil(0.95 * len(values)) - 1)]

    indexing_duration_ms = build_time_ms if indexing_duration_ms is None else indexing_duration_ms
    throughput = (
        chunk_count / (indexing_duration_ms / 1000.0)
        if chunk_count > 0 and indexing_duration_ms > 0
        else None
    )
    measurements = {
        "latency_p50_ms": statistics.median(sorted_latencies) if sorted_latencies else None,
        "latency_p95_ms": p95(sorted_latencies),
        "cold_first_query_latency_ms": latencies_ms[0] if latencies_ms else None,
        "warm_latency_p50_ms": statistics.median(warm_latencies) if warm_latencies else None,
        "warm_latency_p95_ms": p95(warm_latencies),
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_status": peak_rss_status,
        "build_time_ms": build_time_ms,
        "indexing_throughput_chunks_per_second": throughput,
        "index_size_bytes": index_size_bytes,
    }
    measurements["measurement_status"] = {
        name: "measured" if measurements[name] is not None else "unavailable"
        for name in (
            "latency_p50_ms",
            "latency_p95_ms",
            "cold_first_query_latency_ms",
            "warm_latency_p50_ms",
            "warm_latency_p95_ms",
            "build_time_ms",
            "indexing_throughput_chunks_per_second",
            "peak_rss_bytes",
            "index_size_bytes",
        )
    }
    return measurements


def _gate_results(overall: dict, slices: dict[str, dict], *, qa_contract_passed: bool = True) -> dict:
    metric_gates = {
        name: overall[name] >= threshold
        for name, threshold in (
            ("parent_recall_at_10", THRESHOLDS["parent_recall_at_10"]),
            (
                "all_required_evidence_recall_at_20",
                THRESHOLDS["all_required_evidence_recall_at_20"],
            ),
            ("ndcg_at_10", THRESHOLDS["ndcg_at_10"]),
            ("mrr_at_10", THRESHOLDS["mrr_at_10"]),
        )
    }
    metric_gates["no_answer_false_answer_rate"] = (
        overall["no_answer_false_answer_rate"] is not None
        and overall["no_answer_false_answer_rate"] <= THRESHOLDS["no_answer_false_answer_rate"]
    )
    language_results = {}
    for language in ("EN", "RU", "ZH"):
        retrieval_passes = all(
            slices[language][metric] >= threshold - THRESHOLDS["max_language_gate_gap"]
            for metric, threshold in (
                ("parent_recall_at_10", THRESHOLDS["parent_recall_at_10"]),
                (
                    "all_required_evidence_recall_at_20",
                    THRESHOLDS["all_required_evidence_recall_at_20"],
                ),
                ("ndcg_at_10", THRESHOLDS["ndcg_at_10"]),
                ("mrr_at_10", THRESHOLDS["mrr_at_10"]),
            )
        )
        language_results[language] = retrieval_passes and (
            slices[language]["no_answer_false_answer_rate"] is not None
            and slices[language]["no_answer_false_answer_rate"]
            <= THRESHOLDS["no_answer_false_answer_rate"]
        )
    return {
        "release_evidence": False,
        "interpretation": "orchestration-only",
        "metric_results": metric_gates,
        "language_results": language_results,
        "qa_contract_passed": qa_contract_passed,
        "passed_for_orchestration": (
            all(metric_gates.values()) and all(language_results.values()) and qa_contract_passed
        ),
    }


def _load_transformer_embedding(
    selection: ModelSelection,
    *,
    cache_root: Path,
    local_files_only: bool,
    trust_remote_code: bool,
):
    if trust_remote_code:
        raise ValueError("remote model code is forbidden")
    model_spec = selection.embedding
    if model_spec["native_library"]["name"] == "sentence-transformers":
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ValueError(
                "model-matrix adapter requires the retrieval-benchmark extra"
            ) from exc
        model = SentenceTransformer(
            model_spec["id"],
            revision=model_spec["revision"],
            cache_folder=str(cache_root),
            local_files_only=local_files_only,
            trust_remote_code=False,
            model_kwargs={"torch_dtype": "float32"},
        )

        def encode(texts, *, batch_size, max_length, pooling, padding_side, truncation_side):
            if pooling != model_spec["inference"]["pooling"]:
                raise ValueError(f"unsupported native pooling: {pooling}")
            model.max_seq_length = max_length
            model.tokenizer.padding_side = padding_side
            model.tokenizer.truncation_side = truncation_side
            return model.encode(
                list(texts),
                batch_size=batch_size,
                convert_to_numpy=True,
                normalize_embeddings=False,
                show_progress_bar=False,
            )

        return encode
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise ValueError("model-matrix adapter requires the retrieval-benchmark extra") from exc
    common = {
        "revision": model_spec["revision"],
        "cache_dir": str(cache_root),
        "local_files_only": local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(model_spec["id"], **common)
    model = AutoModel.from_pretrained(model_spec["id"], torch_dtype=torch.float32, **common)
    model.eval()
    sparse_linear = None
    if model_spec["id"] == "BAAI/bge-m3":
        from huggingface_hub import hf_hub_download

        asset = Path(
            hf_hub_download(
                repo_id=model_spec["id"],
                filename="sparse_linear.pt",
                revision=model_spec["revision"],
                cache_dir=str(cache_root),
                local_files_only=local_files_only,
            )
        )
        resolved_asset = asset.resolve(strict=True)
        resolved_cache = cache_root.resolve(strict=True)
        if resolved_asset != resolved_cache and resolved_cache not in resolved_asset.parents:
            raise ValueError("BGE-M3 sparse asset escaped the model cache")
        if _sha256_file(resolved_asset) != BGE_M3_SPARSE_LINEAR_SHA256:
            raise ValueError("BGE-M3 sparse_linear.pt SHA256 mismatch")
        state = torch.load(resolved_asset, map_location="cpu", weights_only=True)
        if not isinstance(state, dict) or set(state) != {"weight"}:
            raise ValueError("BGE-M3 sparse_linear.pt has an invalid state dictionary")
        weight = state["weight"]
        if tuple(weight.shape) != (1, selection.variant["dimensions"]):
            raise ValueError("BGE-M3 sparse_linear.pt has an invalid shape")
        sparse_linear = torch.nn.Linear(selection.variant["dimensions"], 1, bias=False)
        sparse_linear.load_state_dict(state, strict=True)
        sparse_linear.eval()

    def encode(texts, *, batch_size, max_length, pooling, padding_side, truncation_side):
        import numpy as np

        tokenizer.padding_side = padding_side
        tokenizer.truncation_side = truncation_side
        chunks = []
        sparse_rows = []
        for offset in range(0, len(texts), batch_size):
            batch = tokenizer(
                list(texts[offset : offset + batch_size]),
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            with torch.inference_mode():
                hidden = model(**batch).last_hidden_state
            mask = batch["attention_mask"]
            if pooling == "cls":
                values = hidden[:, 0]
            elif pooling == "mean":
                expanded = mask.unsqueeze(-1).to(hidden.dtype)
                values = (hidden * expanded).sum(1) / expanded.sum(1).clamp_min(1)
            elif pooling == "last_token":
                reversed_mask = torch.flip(mask, dims=[1])
                indexes = mask.shape[1] - 1 - reversed_mask.argmax(dim=1)
                values = hidden[torch.arange(hidden.shape[0]), indexes]
            else:
                raise ValueError(f"unsupported matrix pooling: {pooling}")
            chunks.append(values.float().cpu().numpy())
            if sparse_linear is not None:
                with torch.inference_mode():
                    token_weights = torch.relu(sparse_linear(hidden)).squeeze(-1).float().cpu()
                unused = {
                    tokenizer.cls_token_id,
                    tokenizer.eos_token_id,
                    tokenizer.pad_token_id,
                    tokenizer.unk_token_id,
                }
                for token_ids, weights in zip(batch["input_ids"].cpu().tolist(), token_weights.tolist()):
                    row = {}
                    for token_id, weight in zip(token_ids, weights):
                        if token_id not in unused and weight > 0:
                            key = str(token_id)
                            row[key] = max(row.get(key, 0.0), float(weight))
                    sparse_rows.append(row)
        dense = np.concatenate(chunks, axis=0)
        if sparse_linear is not None:
            return {"dense_vecs": dense, "lexical_weights": sparse_rows}
        return dense

    return encode


def _qwen_reranker_input_ids(tokenizer, formatting: dict, query: str, document: str, max_length: int):
    query_marker = "__LLM_WIKI_QUERY__"
    document_marker = "__LLM_WIKI_DOCUMENT__"
    rendered = formatting["user_template"].format(
        instruction=formatting["instruction"],
        query=query_marker,
        document=document_marker,
    )
    before_query, marker, remainder = rendered.partition(query_marker)
    if not marker:
        raise ValueError("Qwen reranker user template lacks query placeholder")
    between, marker, after_document = remainder.partition(document_marker)
    if not marker:
        raise ValueError("Qwen reranker user template lacks document placeholder")

    def token_ids(text: str) -> list[int]:
        return list(tokenizer(text, add_special_tokens=False)["input_ids"])

    fixed_prefix = token_ids(formatting["system_prefix"] + before_query)
    fixed_middle = token_ids(between)
    fixed_suffix = token_ids(after_document + formatting["assistant_suffix"])
    query_ids = token_ids(formatting["query_template"].format(query=query))
    document_ids = token_ids(formatting["document_template"].format(document=document))
    fixed_length = len(fixed_prefix) + len(fixed_middle) + len(fixed_suffix)
    if fixed_length > max_length:
        raise ValueError("reranker fixed prefix and suffix exceed matrix max length")
    budget = max_length - fixed_length
    while len(query_ids) + len(document_ids) > budget:
        if len(query_ids) >= len(document_ids):
            query_ids.pop()
        else:
            document_ids.pop()
    return {
        "input_ids": fixed_prefix + query_ids + fixed_middle + document_ids + fixed_suffix,
        "query_tokens_kept": len(query_ids),
        "document_tokens_kept": len(document_ids),
    }


def _load_transformer_reranker(
    selection: ModelSelection,
    *,
    cache_root: Path,
    local_files_only: bool,
    trust_remote_code: bool,
):
    try:
        import torch
        from transformers import (
            AutoModelForCausalLM,
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )
    except ImportError as exc:
        raise ValueError("reranker requires the retrieval-benchmark extra") from exc
    if trust_remote_code:
        raise ValueError("remote model code is forbidden")
    spec = selection.reranker
    formatting = spec["formatting"]
    common = {
        "revision": spec["revision"],
        "cache_dir": str(cache_root),
        "local_files_only": local_files_only,
        "trust_remote_code": False,
    }
    tokenizer = AutoTokenizer.from_pretrained(spec["id"], **common)
    model_class = (
        AutoModelForSequenceClassification
        if formatting["contract_type"] == "tokenizer_pair_sequence_classification"
        else AutoModelForCausalLM
    )
    model = model_class.from_pretrained(spec["id"], torch_dtype=torch.float32, **common)
    model.eval()

    def score(inputs, *, batch_size, max_length, contract_type, score_tokens):
        scores = []
        for offset in range(0, len(inputs), batch_size):
            chunk = inputs[offset : offset + batch_size]
            if contract_type == "tokenizer_pair_sequence_classification":
                queries, documents = zip(*chunk)
                encoded = tokenizer(
                    list(queries),
                    list(documents),
                    padding=True,
                    truncation="longest_first",
                    max_length=max_length,
                    return_tensors="pt",
                )
                with torch.inference_mode():
                    logits = model(**encoded).logits.float().reshape(-1)
                scores.extend(torch.sigmoid(logits).cpu().tolist())
                continue
            rows = [
                _qwen_reranker_input_ids(
                    tokenizer,
                    formatting,
                    item["query"],
                    item["document"],
                    max_length,
                )["input_ids"]
                for item in chunk
            ]
            pad_id = tokenizer.pad_token_id
            if pad_id is None:
                pad_id = tokenizer.eos_token_id
            width = max(len(row) for row in rows)
            input_ids = torch.tensor([[pad_id] * (width - len(row)) + row for row in rows])
            attention = torch.tensor(
                [[0] * (width - len(row)) + [1] * len(row) for row in rows]
            )
            with torch.inference_mode():
                logits = model(input_ids=input_ids, attention_mask=attention).logits[:, -1, :].float()
            no_ids = tokenizer(score_tokens["negative"], add_special_tokens=False)["input_ids"]
            yes_ids = tokenizer(score_tokens["positive"], add_special_tokens=False)["input_ids"]
            if len(no_ids) != 1 or len(yes_ids) != 1:
                raise ValueError("Qwen reranker yes/no score tokens must each be one token")
            pair = torch.stack((logits[:, no_ids[0]], logits[:, yes_ids[0]]), dim=1)
            scores.extend(torch.softmax(pair, dim=1)[:, 1].cpu().tolist())
        return scores

    return score


def _fuse_rankings(
    rankings: Sequence[Sequence[ScoredCandidate]], candidates: Sequence[Candidate], *, limit: int
) -> list[ScoredCandidate]:
    by_id = {candidate.evidence_id: candidate for candidate in candidates}
    fused = _reciprocal_rank_fusion(
        {
            f"ranking-{index}": [(item.evidence_id, item.score) for item in ranking]
            for index, ranking in enumerate(rankings)
        },
        k=60,
    )
    return [ScoredCandidate(by_id[evidence_id], score) for evidence_id, score in fused[:limit]]


def _remove_semantic_artifacts(cache_root: Path, lexical_path: Path) -> None:
    for path in cache_root.iterdir():
        if path == lexical_path:
            continue
        if _is_reparse_point(path):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


def _decorate_lexical_fallback(
    report: dict,
    *,
    selection: ModelSelection,
    lexical_config: str,
    vector_backend: str,
    acquisition_mode: str | None,
    reason: str,
) -> dict:
    report["adapter_kind"] = MODEL_MATRIX_ADAPTER_KIND
    report["quality_claim"] = False
    report["release_evidence"] = False
    report["requested_mode"] = MODEL_MATRIX_ADAPTER_KIND
    report["effective_mode"] = f"lexical-{lexical_config}"
    report["fallback_reason"] = reason
    report["model_id"] = selection.embedding["id"]
    report["variant_id"] = selection.variant["variant_id"]
    report["revision"] = selection.embedding["revision"]
    report["matrix_sha256"] = selection.matrix_sha256
    report["corpus_sha256"] = selection.corpus_sha256
    report["acquisition_mode"] = acquisition_mode
    report["vector_backend"] = vector_backend
    report["reranker"] = None
    report["benchmark_contract_sha256"] = _sha256_json(selection.matrix["benchmark_contract"])
    report["benchmark_runner_sha256"] = _sha256_file(Path(__file__))
    report["candidate"] = {
        "embedding": _matrix_target(selection.embedding, selection.variant),
        "reranker": (
            _matrix_target(selection.reranker, selection.reranker_variant)
            if selection.reranker is not None
            else None
        ),
    }
    report["gates"]["degraded"] = True
    report["gates"]["release_evidence"] = False
    report["gates"]["interpretation"] = "lexical-fallback"
    report["methodology"]["requested_retrieval_order"] = (
        "independent BM25 and dense generation with optional all-depth reranking"
    )
    return report


def prefetch_models(selection: ModelSelection, *, cache_root: Path | str) -> dict:
    cache_root = _validate_cache_root(Path(cache_root))
    with model_cache_environment(cache_root, allow_download=True) as acquisition_mode:
        _load_transformer_embedding(
            selection,
            cache_root=cache_root.resolve(),
            local_files_only=False,
            trust_remote_code=False,
        )
        if selection.reranker is not None:
            _load_transformer_reranker(
                selection,
                cache_root=cache_root.resolve(),
                local_files_only=False,
                trust_remote_code=False,
            )
    return {
        "schema_version": 1,
        "artifact_kind": "model-acquisition-receipt",
        "quality_claim": False,
        "release_evidence": False,
        "acquisition_mode": acquisition_mode,
        "candidate": {
            "embedding": _matrix_target(selection.embedding, selection.variant),
            "reranker": (
                _matrix_target(selection.reranker, selection.reranker_variant)
                if selection.reranker is not None
                else None
            ),
        },
        "matrix_sha256": selection.matrix_sha256,
        "corpus_sha256": selection.corpus_sha256,
        "environment_provenance": _environment_provenance(None),
    }


@dataclass(frozen=True)
class _RunWorkspace:
    path: Path
    identity: os.stat_result
    runs_root: Path
    runs_identity: os.stat_result
    model_cache_identity: str


def _owner_only_directory(path: Path, label: str) -> os.stat_result:
    if _is_reparse_point(path):
        raise ValueError(f"{label} must not be a symlink or reparse point")
    identity = path.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and identity.st_uid != os.getuid():
        raise PermissionError(f"{label} is not owned by the current user")
    if os.name == "posix" and stat.S_IMODE(identity.st_mode) & 0o077:
        raise PermissionError(f"{label} must be owner-controlled")
    return identity


def _create_run_workspace(model_cache_root: Path) -> _RunWorkspace:
    requested = model_cache_root.expanduser()
    if requested.exists() and _is_reparse_point(requested):
        raise ValueError("model cache root must not be a symlink or reparse point")
    requested.mkdir(parents=True, exist_ok=True)
    root = _validate_cache_root(requested).resolve(strict=True)
    if _is_reparse_point(root):
        raise ValueError("model cache root must not be a symlink or reparse point")
    root_identity = root.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and root_identity.st_uid != os.getuid():
        raise PermissionError("model cache root is not owned by the current user")
    runs_root = root / "runs"
    if runs_root.exists() and _is_reparse_point(runs_root):
        raise ValueError("runs directory must not be a symlink or reparse point")
    runs_root.mkdir(mode=0o700, exist_ok=True)
    runs_identity = _owner_only_directory(runs_root, "runs directory")
    workspace = None
    for _attempt in range(32):
        candidate = runs_root / secrets.token_hex(16)
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        workspace = candidate
        break
    if workspace is None:
        raise FileExistsError("could not allocate a unique benchmark run workspace")
    workspace_identity = _owner_only_directory(workspace, "run workspace")
    if not _same_path_identity(runs_root, runs_identity):
        shutil.rmtree(workspace)
        raise PermissionError("runs directory changed during workspace creation")
    identity_payload = {
        "device": int(root_identity.st_dev),
        "inode": int(root_identity.st_ino),
    }
    return _RunWorkspace(
        workspace,
        workspace_identity,
        runs_root,
        runs_identity,
        _sha256_json(identity_payload),
    )


def _cleanup_run_workspace(workspace: _RunWorkspace) -> None:
    if not _same_path_identity(workspace.runs_root, workspace.runs_identity):
        raise PermissionError("runs directory changed before workspace cleanup")
    if _is_reparse_point(workspace.path) or not _same_path_identity(
        workspace.path, workspace.identity
    ):
        raise PermissionError("run workspace changed before cleanup")
    if os.name == "posix" and not shutil.rmtree.avoids_symlink_attacks:
        raise PermissionError("platform lacks descriptor-safe workspace cleanup")
    shutil.rmtree(workspace.path)


def run_benchmark(
    corpus: dict,
    *,
    cache_root: Path | str,
    adapter: str = ADAPTER_KIND,
    corpus_path: Path | str = DEFAULT_CORPUS,
    matrix_path: Path | str = DEFAULT_MATRIX,
    model_id: str | None = None,
    variant_id: str | None = None,
    reranker_id: str | None = None,
    rerank_depth: int | None = None,
    vector_backend: str | None = None,
    allow_download: bool = False,
    lexical_config: str | None = None,
    test_segmenter=None,
    model_loader=None,
    encoder=None,
    reranker_loader=None,
    scorer=None,
    usearch_search=None,
    raw_output_written: bool = False,
    lexical_deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
    clock=None,
) -> dict:
    if adapter != MODEL_MATRIX_ADAPTER_KIND:
        return _run_benchmark_once(
            corpus,
            cache_root=cache_root,
            adapter=adapter,
            corpus_path=corpus_path,
            matrix_path=matrix_path,
            model_id=model_id,
            variant_id=variant_id,
            reranker_id=reranker_id,
            rerank_depth=rerank_depth,
            vector_backend=vector_backend,
            allow_download=allow_download,
            lexical_config=lexical_config,
            test_segmenter=test_segmenter,
            model_loader=model_loader,
            encoder=encoder,
            reranker_loader=reranker_loader,
            scorer=scorer,
            usearch_search=usearch_search,
            raw_output_written=raw_output_written,
            lexical_deadline_seconds=lexical_deadline_seconds,
            clock=clock,
        )
    workspace = _create_run_workspace(Path(cache_root))
    try:
        report = _run_benchmark_once(
            corpus,
            cache_root=workspace.path,
            model_cache_root=Path(cache_root).expanduser().resolve(strict=True),
            adapter=adapter,
            corpus_path=corpus_path,
            matrix_path=matrix_path,
            model_id=model_id,
            variant_id=variant_id,
            reranker_id=reranker_id,
            rerank_depth=rerank_depth,
            vector_backend=vector_backend,
            allow_download=allow_download,
            lexical_config=lexical_config,
            test_segmenter=test_segmenter,
            model_loader=model_loader,
            encoder=encoder,
            reranker_loader=reranker_loader,
            scorer=scorer,
            usearch_search=usearch_search,
            raw_output_written=raw_output_written,
            lexical_deadline_seconds=lexical_deadline_seconds,
            clock=clock,
        )
        report["methodology"]["cache_isolation"] = {
            "model_cache_root_identity": workspace.model_cache_identity,
            "model_cache_persistent": True,
            "run_workspace": "owner-only ephemeral child of cache-root/runs",
            "cleanup": "always after measurement",
        }
        return report
    finally:
        _cleanup_run_workspace(workspace)


def _run_benchmark_once(
    corpus: dict,
    *,
    cache_root: Path | str,
    adapter: str = ADAPTER_KIND,
    corpus_path: Path | str = DEFAULT_CORPUS,
    matrix_path: Path | str = DEFAULT_MATRIX,
    model_id: str | None = None,
    variant_id: str | None = None,
    reranker_id: str | None = None,
    rerank_depth: int | None = None,
    vector_backend: str | None = None,
    allow_download: bool = False,
    lexical_config: str | None = None,
    test_segmenter=None,
    model_loader=None,
    encoder=None,
    reranker_loader=None,
    scorer=None,
    usearch_search=None,
    raw_output_written: bool = False,
    lexical_deadline_seconds: float = DEFAULT_LEXICAL_DEADLINE_SECONDS,
    clock=None,
    model_cache_root: Path | str | None = None,
) -> dict:
    """Run retrieval without exposing query gold metadata to any adapter."""
    if adapter not in {ADAPTER_KIND, MODEL_MATRIX_ADAPTER_KIND}:
        raise ValueError(
            "only deterministic-fake or explicit model-matrix adapters are supported"
        )
    real_mode = adapter == MODEL_MATRIX_ADAPTER_KIND
    if real_mode and not all((model_id, variant_id, lexical_config, vector_backend)):
        raise ValueError("model-matrix mode requires explicit model, variant, lexical, and vector choices")
    if real_mode and rerank_depth is not None:
        raise ValueError("reranker evidence always runs matrix depths 10, 20, and 50 together")
    if lexical_config is not None and lexical_config not in LEXICAL_CONFIGURATIONS:
        raise ValueError(f"unknown lexical configuration: {lexical_config}")
    cache_root = _validate_cache_root(Path(cache_root))
    persistent_model_cache = _validate_cache_root(
        Path(model_cache_root) if model_cache_root is not None else cache_root
    )
    candidates = build_candidates(corpus)
    phase_clock = clock or time.perf_counter
    build_started = time.perf_counter()
    lexical_started = phase_clock()
    lexical_deadline = build_started + lexical_deadline_seconds
    sqlite_lexical = None
    selection = None
    acquisition_mode = None
    model_load_ms = None
    document_encoding_ms = None
    vector_bytes = None
    learned_sparse_bytes = None
    reranker_model_load_ms = None
    materialized_retrieval = None
    cache_context = None
    effective_real_mode = real_mode
    fallback_reason = None
    if not real_mode and lexical_config is None:
        index_size = _write_fake_index(corpus, cache_root)
        lexical = FakeLexicalAdapter()
        embedding = FakeEmbeddingAdapter()
        reranker = FakeRerankerAdapter()
    else:
        sqlite_lexical = SQLiteLexicalAdapter(
            candidates,
            cache_root,
            lexical_config,
            test_segmenter=test_segmenter,
            deadline=lexical_deadline,
        )
        lexical = sqlite_lexical
        embedding = None
        reranker = None
        index_size = sqlite_lexical.path.stat().st_size
    lexical_build_ms = (phase_clock() - lexical_started) * 1000
    if effective_real_mode:
        try:
            selection = load_model_selection(
                matrix_path,
                corpus_path,
                model_id=model_id,
                variant_id=variant_id,
                reranker_id=reranker_id,
            )
            if hashlib.sha256(canonical_json_bytes(corpus) + b"\n").hexdigest() != selection.corpus_sha256:
                raise ValueError("loaded corpus content does not match matrix corpus SHA256")
        except BaseException:
            if sqlite_lexical is not None:
                sqlite_lexical._cleanup(remove=True)
            raise
        try:
            cache_context = model_cache_environment(
                persistent_model_cache, allow_download=allow_download
            )
            acquisition_mode = cache_context.__enter__()
            load_started = phase_clock()
            if encoder is not None:
                dense_encoder = encoder
            elif model_loader is not None:
                dense_encoder = model_loader(
                    selection,
                    cache_root=persistent_model_cache.resolve(),
                    local_files_only=not allow_download,
                    trust_remote_code=False,
                )
            else:
                dense_encoder = _load_transformer_embedding(
                    selection,
                    cache_root=persistent_model_cache.resolve(),
                    local_files_only=not allow_download,
                    trust_remote_code=False,
                )
            embedding = ModelEmbeddingAdapter(
                selection,
                encoder=dense_encoder,
                vector_backend=vector_backend,
                usearch_search=usearch_search,
            )
            model_load_ms = (phase_clock() - load_started) * 1000
            document_started = phase_clock()
            embedding._ensure_documents(candidates)
            document_encoding_ms = (phase_clock() - document_started) * 1000
            vector_bytes = int(embedding.document_vectors.nbytes)
            learned_sparse_bytes = embedding.learned_sparse_bytes
            index_size += vector_bytes + learned_sparse_bytes
        except Exception as exc:
            if cache_context is not None:
                cache_context.__exit__(*sys.exc_info())
                cache_context = None
            fallback_reason = f"{type(exc).__name__}: {exc}"
            effective_real_mode = False
            embedding = None
            reranker = None
            model_load_ms = None
            document_encoding_ms = None
            _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
            index_size = sqlite_lexical.path.stat().st_size
        except BaseException:
            if cache_context is not None:
                cache_context.__exit__(*sys.exc_info())
                cache_context = None
            sqlite_lexical._cleanup(remove=True)
            _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
            raise
    build_time_ms = (time.perf_counter() - build_started) * 1000

    qa = FakeQAAdapter()
    rows = []
    traces = []
    latencies = []
    rerank_rows = {
        depth: []
        for depth in (selection.matrix["benchmark_contract"]["reranker_depths"] if real_mode else [])
    }

    if effective_real_mode:
        releasing_embedding = False
        try:
            import numpy as np

            materialized_retrieval = []
            for query in corpus["queries"]:
                started = time.perf_counter()
                scope = _query_scope(query)
                lexical_ranked = sqlite_lexical.rank(
                    query["text"],
                    scope,
                    candidates,
                    limit=MAX_CANDIDATES,
                    language=query["language"],
                )
                embedding_ranked = embedding.rank(
                    query["text"], scope, candidates, limit=MAX_CANDIDATES
                )
                fused = _fuse_rankings(
                    (lexical_ranked, embedding_ranked), candidates, limit=MAX_CANDIDATES
                )
                materialized_retrieval.append(
                    {
                        "candidate_ids": tuple(item.evidence_id for item in fused),
                        "scores": np.asarray(
                            [item.score for item in fused], dtype=np.float32
                        ),
                        "embedding_signals": json.loads(json.dumps(embedding.last_trace)),
                        "duration_ms": (time.perf_counter() - started) * 1000,
                    }
                )

            embedding._encoder = None
            dense_encoder = None
            embedding = None
            releasing_embedding = True
            gc.collect()
            torch = sys.modules.get("torch")
            if torch is not None:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                mps = getattr(torch, "mps", None)
                if mps is not None and mps.is_available():
                    mps.empty_cache()
            releasing_embedding = False

            if reranker_id is not None:
                reranker_load_started = phase_clock()
                if scorer is not None:
                    reranker_scorer = scorer
                elif reranker_loader is not None:
                    reranker_scorer = reranker_loader(
                        selection,
                        cache_root=persistent_model_cache.resolve(),
                        local_files_only=not allow_download,
                        trust_remote_code=False,
                    )
                else:
                    reranker_scorer = _load_transformer_reranker(
                        selection,
                        cache_root=persistent_model_cache.resolve(),
                        local_files_only=not allow_download,
                        trust_remote_code=False,
                    )
                reranker = ModelRerankerAdapter(selection, scorer=reranker_scorer)
                reranker_model_load_ms = (phase_clock() - reranker_load_started) * 1000
                model_load_ms += reranker_model_load_ms
        except Exception as exc:
            if releasing_embedding:
                if cache_context is not None:
                    cache_context.__exit__(*sys.exc_info())
                    cache_context = None
                sqlite_lexical._cleanup(remove=True)
                _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
                raise
            if cache_context is not None:
                cache_context.__exit__(*sys.exc_info())
                cache_context = None
            reason = f"{type(exc).__name__}: {exc}"
            sqlite_lexical._cleanup(remove=True)
            _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
            fallback = run_benchmark(
                corpus,
                cache_root=cache_root,
                adapter=ADAPTER_KIND,
                lexical_config=lexical_config,
                test_segmenter=test_segmenter,
                lexical_deadline_seconds=lexical_deadline_seconds,
            )
            return _decorate_lexical_fallback(
                fallback,
                selection=selection,
                lexical_config=lexical_config,
                vector_backend=vector_backend,
                acquisition_mode=acquisition_mode,
                reason=reason,
            )
        except BaseException:
            if cache_context is not None:
                cache_context.__exit__(*sys.exc_info())
                cache_context = None
            sqlite_lexical._cleanup(remove=True)
            _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
            raise

    lexical_run_succeeded = False
    try:
        for query_index, query in enumerate(corpus["queries"]):
            started = time.perf_counter()
            scope = _query_scope(query)
            pre_rerank_ids = None
            reranker_trace = None
            if not real_mode and sqlite_lexical is None:
                lexical_ranked = lexical.rank(
                    query["text"], scope, candidates, limit=MAX_CANDIDATES
                )
                assert embedding is not None and reranker is not None
                embedding_ranked = embedding.rank(
                    query["text"], scope, candidates, limit=MAX_CANDIDATES
                )
                ranked = reranker.rank(
                    query["text"], scope, lexical_ranked + embedding_ranked, limit=MAX_CANDIDATES
                )
            elif effective_real_mode:
                frozen = materialized_retrieval[query_index]
                ranked = [
                    ScoredCandidate(sqlite_lexical._candidates[evidence_id], float(score))
                    for evidence_id, score in zip(frozen["candidate_ids"], frozen["scores"])
                ]
                retrieval_duration_ms = frozen["duration_ms"]
                pre_rerank_ids = list(frozen["candidate_ids"])
                embedding_signals = frozen["embedding_signals"]
                try:
                    if reranker is not None:
                        reranking_started = time.perf_counter()
                        ranked_by_depth = reranker.rank_all(
                            query["text"], scope, ranked, limit=MAX_CANDIDATES
                        )
                        for depth, depth_ranking in ranked_by_depth.items():
                            reranker.last_trace["depths"][str(depth)]["ranked_evidence"] = [
                                {"evidence_id": item.evidence_id, "score": round(item.score, 8)}
                                for item in depth_ranking
                            ]
                            reranker.last_trace["depths"][str(depth)]["query_latency_ms"] = (
                                retrieval_duration_ms
                                + reranker.last_trace["depths"][str(depth)]["duration_ms"]
                            )
                            depth_answer = qa.answer(query["text"], scope, depth_ranking)
                            reranker.last_trace["depths"][str(depth)]["abstained"] = depth_answer[
                                "abstained"
                            ]
                            reranker.last_trace["depths"][str(depth)]["abstention_reason"] = (
                                depth_answer["reason"]
                            )
                            rerank_rows[depth].append(
                                _evaluation_row(
                                    query,
                                    depth_ranking,
                                    depth_answer,
                                )
                            )
                        ranked = ranked_by_depth[max(ranked_by_depth)]
                        reranker_trace = json.loads(json.dumps(reranker.last_trace))
                        real_latency_ms = retrieval_duration_ms + (
                            time.perf_counter() - reranking_started
                        ) * 1000
                    else:
                        real_latency_ms = retrieval_duration_ms
                except Exception as exc:
                    if cache_context is not None:
                        cache_context.__exit__(*sys.exc_info())
                        cache_context = None
                    reason = f"{type(exc).__name__}: {exc}"
                    sqlite_lexical._cleanup(remove=True)
                    _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
                    fallback = run_benchmark(
                        corpus,
                        cache_root=cache_root,
                        adapter=ADAPTER_KIND,
                        lexical_config=lexical_config,
                        test_segmenter=test_segmenter,
                        lexical_deadline_seconds=lexical_deadline_seconds,
                    )
                    return _decorate_lexical_fallback(
                        fallback,
                        selection=selection,
                        lexical_config=lexical_config,
                        vector_backend=vector_backend,
                        acquisition_mode=acquisition_mode,
                        reason=reason,
                    )
                except BaseException:
                    if cache_context is not None:
                        cache_context.__exit__(*sys.exc_info())
                        cache_context = None
                    sqlite_lexical._cleanup(remove=True)
                    _remove_semantic_artifacts(cache_root.resolve(), sqlite_lexical.path)
                    raise
            else:
                ranked = sqlite_lexical.rank(
                    query["text"], scope, candidates, limit=MAX_CANDIDATES, language=query["language"]
                )
            answer = qa.answer(query["text"], scope, ranked)
            latency_ms = (
                real_latency_ms
                if effective_real_mode
                else (time.perf_counter() - started) * 1000
            )
            latencies.append(latency_ms)
            ranked_parents = _expand_parents(ranked)
            row = _evaluation_row(query, ranked, answer)
            rows.append(row)
            trace = {
                    "query_id": query["query_id"],
                    "ranked_evidence": [
                        {"evidence_id": item.evidence_id, "score": round(item.score, 8)}
                        for item in ranked
                    ],
                    "ranked_parents": ranked_parents,
                    "abstained": answer["abstained"],
                    "abstention_reason": answer["reason"],
                    "abstention_contract_valid": row["abstention_contract_valid"],
                    "latency_ms": round(latency_ms, 6),
                }
            if real_mode:
                trace["pre_rerank_candidate_ids"] = pre_rerank_ids
                trace["reranker"] = reranker_trace
                trace["embedding_signals"] = embedding_signals if effective_real_mode else None
            traces.append(trace)
        lexical_run_succeeded = True
    finally:
        if sqlite_lexical is not None:
            sqlite_lexical._cleanup(remove=not lexical_run_succeeded)
        if cache_context is not None:
            cache_context.__exit__(*sys.exc_info())

    overall = _aggregate(rows)
    slices = {
        language: _aggregate([row for row in rows if row["language"] == language])
        for language in ("EN", "RU", "ZH")
    }
    slices["cross-language"] = _aggregate([row for row in rows if row["cross_language"]])
    macro = {metric: macro_average(slices, metric) for metric in EFFECTIVENESS_FIELDS}
    peak_rss, peak_rss_status = _peak_rss()
    measurements = _resource_measurements(
        latencies,
        build_time_ms=build_time_ms,
        indexing_duration_ms=(
            document_encoding_ms if effective_real_mode else lexical_build_ms
        ),
        chunk_count=len(candidates),
        index_size_bytes=index_size,
        peak_rss_bytes=peak_rss,
        peak_rss_status=peak_rss_status,
    )
    measurements["lexical_build_ms"] = lexical_build_ms
    measurements["measurement_status"]["lexical_build_ms"] = "measured"
    if effective_real_mode:
        measurements["model_load_ms"] = model_load_ms
        measurements["reranker_model_load_ms"] = reranker_model_load_ms
        measurements["document_encoding_index_build_ms"] = document_encoding_ms
        measurements["vector_bytes"] = vector_bytes
        measurements["learned_sparse_bytes"] = learned_sparse_bytes
        measurements["vector_bytes_per_document"] = (
            vector_bytes / len(candidates) if candidates else 0
        )
        measurements["measurement_status"].update(
            model_load_ms="measured" if model_load_ms is not None else "unavailable",
            reranker_model_load_ms=(
                "measured" if reranker_model_load_ms is not None else "not-applicable"
            ),
            document_encoding_index_build_ms=(
                "measured" if document_encoding_ms is not None else "unavailable"
            ),
            vector_bytes="measured",
            learned_sparse_bytes=(
                "measured" if learned_sparse_bytes is not None else "not-applicable"
            ),
            vector_bytes_per_document="measured",
        )
    methodology = {
        "source": "https://ir-measur.es/en/latest/measures.html",
        "positive_retrieval_denominator": "answerable queries only",
        "recall": "fraction of all known relevant candidates retrieved",
        "all_required": "one only when every required evidence span is in top 20",
        "mrr": "reciprocal rank of first relevant evidence span through rank 10",
        "ndcg": "graded gain with log2 discount normalized by ideal ranking through rank 10",
        "false_answer_denominator": "unanswerable queries only",
        "unavailable_metrics": (
            "empty-denominator rates are represented as JSON null and excluded from macro means"
        ),
        "timing_and_rss": "measured and non-canonical",
        "phase_peak_rss": "process peak RSS is run-level; per-phase peaks are unavailable",
        "resource_unavailable": "unavailable measurements use JSON null plus measurement_status",
        "environment_provenance": _environment_provenance(vector_backend if real_mode else None),
    }
    if lexical_config is not None:
        methodology["lexical_configuration"] = json.loads(
            json.dumps(LEXICAL_CONFIGURATIONS[lexical_config])
        )
        methodology["retrieval_order"] = (
            "independent BM25 and dense generation, RRF, then all frozen-prefix rerank depths"
            if effective_real_mode
            else "BM25-only lexical ablation; dense models not invoked"
        )
        methodology["segmentation_runtime"] = sqlite_lexical.segmentation_runtime
        methodology["confidence"] = {
            "fusion": "RRF divided by its theoretical maximum; bounded to [0,1]",
            "qa_threshold": 0.54,
            "reranker": (
                "sigmoid_probability_from_sequence_classification_logit"
                if effective_real_mode
                and selection.reranker is not None
                and selection.reranker["formatting"]["contract_type"]
                == "tokenizer_pair_sequence_classification"
                else selection.reranker["formatting"]["scoring"]
                if effective_real_mode and selection.reranker is not None
                else None
            ),
        }
    gates = _gate_results(
        overall,
        slices,
        qa_contract_passed=all(row["abstention_contract_valid"] for row in rows),
    )
    quality_claim = False
    release_evidence = False
    if real_mode:
        gates["interpretation"] = "real-model-quality" if quality_claim else "test-or-experimental"
        gates["shipping_eligible"] = True
        gates["overall"] = gates["passed_for_orchestration"]
        gates["per_language"] = dict(gates["language_results"])
        gates["no_parent_recall_at_10_regression"] = gates["metric_results"][
            "parent_recall_at_10"
        ]
        gates["required"] = {
            "every_language_gate": all(gates["language_results"].values()),
            "latency": measurements["measurement_status"]["warm_latency_p95_ms"] == "measured",
            "license": selection.embedding["license"] in {"Apache-2.0", "MIT"}
            and (selection.reranker is None or selection.reranker["license"] in {"Apache-2.0", "MIT"}),
            "no_parent_recall_at_10_regression": gates["metric_results"]["parent_recall_at_10"],
            "ram": measurements["measurement_status"]["peak_rss_bytes"] == "measured",
        }
        gates["degraded"] = not effective_real_mode
        gates["release_evidence"] = False
    depth_metrics = {}
    if effective_real_mode and selection.reranker is not None:
        for depth, depth_rows in rerank_rows.items():
            depth_slices = {
                language: _aggregate(
                    [row for row in depth_rows if row["language"] == language]
                )
                for language in ("EN", "RU", "ZH")
            }
            depth_slices["cross-language"] = _aggregate(
                [row for row in depth_rows if row["cross_language"]]
            )
            depth_metrics[str(depth)] = {
                "overall": _aggregate(depth_rows),
                "slices": depth_slices,
                "duration_ms": sum(
                    trace["reranker"]["depths"][str(depth)]["duration_ms"]
                    for trace in traces
                ),
                "inference_latencies_ms": [
                    trace["reranker"]["depths"][str(depth)]["duration_ms"]
                    for trace in traces
                ],
                "warm_latency_p95_ms": _resource_measurements(
                    [
                        trace["reranker"]["depths"][str(depth)]["query_latency_ms"]
                        for trace in traces
                    ],
                    build_time_ms=0,
                    chunk_count=0,
                    index_size_bytes=0,
                    peak_rss_bytes=None,
                    peak_rss_status="shared-run-level",
                )["warm_latency_p95_ms"],
                "shared_resources": ["peak_rss_bytes", "index_size_bytes", "vector_bytes"],
            }
    lexical_matrix = None
    if lexical_config is not None and not real_mode:
        matrix_raw = read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix")
        lexical_matrix = json.loads(matrix_raw)
        if canonical_json_bytes(lexical_matrix) + b"\n" != matrix_raw:
            raise ValueError("model matrix bytes are not canonical and frozen")
        if _sha256_file(Path(corpus_path)) != lexical_matrix["benchmark_contract"]["corpus"]["sha256"]:
            raise ValueError("lexical ablation corpus does not match the benchmark contract")
    return {
        "schema_version": "retrieval-report/v2",
        "corpus_id": corpus["corpus_id"],
        "adapter_kind": MODEL_MATRIX_ADAPTER_KIND if real_mode else (
            sqlite_lexical.kind if sqlite_lexical is not None else ADAPTER_KIND
        ),
        "quality_claim": quality_claim,
        "methodology": methodology,
        "overall": overall,
        "slices": slices,
        "macro_average": macro,
        "thresholds": dict(THRESHOLDS),
        "gates": gates,
        "measurements": measurements,
        "traces": traces,
        "model_id": selection.embedding["id"] if real_mode else None,
        "variant_id": selection.variant["variant_id"] if real_mode else None,
        "revision": selection.embedding["revision"] if real_mode else None,
        "matrix_sha256": (
            selection.matrix_sha256
            if real_mode
            else _sha256_file(Path(matrix_path)) if lexical_matrix is not None else None
        ),
        "corpus_sha256": (
            selection.corpus_sha256
            if real_mode
            else _sha256_file(Path(corpus_path)) if lexical_matrix is not None else None
        ),
        "acquisition_mode": acquisition_mode,
        "vector_backend": vector_backend if real_mode else None,
        "reranker": (
            {
                "model_id": selection.reranker["id"],
                "revision": selection.reranker["revision"],
                "variant_id": selection.reranker_variant["variant_id"],
                "depths": list(selection.matrix["benchmark_contract"]["reranker_depths"]),
                "depth_metrics": depth_metrics,
                "formatting": selection.reranker["formatting"],
            }
            if real_mode and selection.reranker is not None and effective_real_mode
            else None
        ),
        "release_evidence": release_evidence,
        "requested_mode": MODEL_MATRIX_ADAPTER_KIND if real_mode else ADAPTER_KIND,
        "effective_mode": (
            MODEL_MATRIX_ADAPTER_KIND
            if effective_real_mode
            else f"lexical-{lexical_config}" if real_mode else ADAPTER_KIND
        ),
        "fallback_reason": fallback_reason,
        "benchmark_contract_sha256": (
            _sha256_json(selection.matrix["benchmark_contract"])
            if real_mode
            else _sha256_json(lexical_matrix["benchmark_contract"])
            if lexical_matrix is not None
            else None
        ),
        "benchmark_runner_sha256": (
            _sha256_file(Path(__file__)) if real_mode or lexical_matrix is not None else None
        ),
        "candidate": (
            {
                "embedding": _matrix_target(selection.embedding, selection.variant),
                "reranker": (
                    _matrix_target(selection.reranker, selection.reranker_variant)
                    if selection.reranker is not None
                    else None
                ),
            }
            if real_mode
            else None
        ),
    }


def _candidate_key(candidate: dict) -> str:
    return json.dumps(candidate, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _candidate_policy(matrix: dict, spec: dict) -> tuple[dict, dict | None]:
    embedding_target = spec["embedding"]
    embedding = next(
        (
            item
            for item in matrix["embeddings"]
            if item["id"] == embedding_target["id"]
            and item["revision"] == embedding_target["revision"]
            and any(
                variant["variant_id"] == embedding_target["variant_id"]
                and variant["dimensions"] == embedding_target["dimensions"]
                for variant in item["variants"]
            )
        ),
        None,
    )
    if embedding is None:
        raise ValueError("raw report selects an unknown embedding target")
    reranker_target = spec["reranker"]
    reranker = None
    if reranker_target is not None:
        reranker = next(
            (
                item
                for item in matrix["rerankers"]
                if item["id"] == reranker_target["id"]
                and item["revision"] == reranker_target["revision"]
                and any(
                    variant["variant_id"] == reranker_target["variant_id"]
                    and variant["dimensions"] == reranker_target["dimensions"]
                    for variant in item["variants"]
                )
            ),
            None,
        )
        if reranker is None:
            raise ValueError("raw report selects an unknown reranker target")
    return embedding, reranker


def _metrics_match(claimed: object, recomputed: object, path: str) -> None:
    if isinstance(recomputed, dict):
        if not isinstance(claimed, dict) or set(claimed) != set(recomputed):
            raise ValueError(f"candidate metric object mismatch at {path}")
        for key, value in recomputed.items():
            _metrics_match(claimed[key], value, f"{path}.{key}")
        return
    if isinstance(recomputed, list):
        if not isinstance(claimed, list) or len(claimed) != len(recomputed):
            raise ValueError(f"candidate metric list mismatch at {path}")
        for index, value in enumerate(recomputed):
            _metrics_match(claimed[index], value, f"{path}[{index}]")
        return
    if recomputed is None or isinstance(recomputed, int):
        if claimed != recomputed or type(claimed) is not type(recomputed):
            raise ValueError(f"candidate metric mismatch at {path}")
        return
    if not isinstance(claimed, (int, float)) or not math.isclose(
        float(claimed), float(recomputed), rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(f"candidate metric mismatch at {path}")


def _recompute_report_metrics(
    corpus: dict,
    report: dict,
    *,
    require_complete_rankings: bool = True,
    require_normalized_confidence: bool = True,
) -> tuple[dict, dict]:
    queries = {query["query_id"]: query for query in corpus["queries"]}
    traces = report["traces"]
    if not isinstance(traces, list) or len(traces) != len(queries):
        raise ValueError("candidate traces must contain every frozen query exactly once")
    trace_ids = [trace.get("query_id") for trace in traces if isinstance(trace, dict)]
    if len(trace_ids) != len(traces) or set(trace_ids) != set(queries):
        raise ValueError("candidate traces contain missing, extra, or duplicate query IDs")
    gold_fields = {
        "answerability",
        "allowed_abstention_reason",
        "cross_language",
        "graded_evidence",
        "language",
        "negative_candidates",
        "project_scope",
        "relevant_parents",
        "required_evidence_spans",
        "temporal_scope",
    }

    def contains_gold(value: object) -> bool:
        if isinstance(value, dict):
            return bool(gold_fields & set(value)) or any(contains_gold(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_gold(item) for item in value)
        return False

    if contains_gold(report):
        raise ValueError("candidate report contains forbidden gold fields")
    confidence = report["methodology"].get("confidence")
    if not isinstance(confidence, dict) or confidence.get("fusion") != (
        "RRF divided by its theoretical maximum; bounded to [0,1]"
    ) or confidence.get("qa_threshold") != 0.54:
        raise ValueError("candidate report confidence source or calibration is not approved")
    approved_reranker_sources = {
        None,
        "sigmoid_probability_from_sequence_classification_logit",
        "softmax_probability_of_yes_over_no",
    }
    if confidence.get("reranker") not in approved_reranker_sources:
        raise ValueError("candidate report reranker confidence source is not approved")

    candidates = build_candidates(corpus)
    by_id = {candidate.evidence_id: candidate for candidate in candidates}
    rows = []
    for trace in traces:
        query = queries[trace["query_id"]]
        ranked_evidence = trace.get("ranked_evidence")
        if not isinstance(ranked_evidence, list) or any(
            not isinstance(item, dict) or set(item) != {"evidence_id", "score"}
            for item in ranked_evidence
        ):
            raise ValueError("candidate trace ranked evidence is malformed")
        ranked_ids = [item["evidence_id"] for item in ranked_evidence]
        eligible = {
            item.evidence_id
            for item in filter_candidates(candidates, _query_scope(query))
        }
        if len(ranked_ids) != len(set(ranked_ids)) or (
            set(ranked_ids) != eligible
            if require_complete_rankings
            else not set(ranked_ids) <= eligible
        ):
            raise ValueError("candidate trace does not contain exact eligible candidate IDs")
        if require_normalized_confidence and any(
            not isinstance(item["score"], (int, float))
            or not math.isfinite(float(item["score"]))
            or not 0 <= float(item["score"]) <= 1
            for item in ranked_evidence
        ):
            raise ValueError("candidate trace scores must be finite normalized confidence")
        ranked = [ScoredCandidate(by_id[evidence_id], 0.0) for evidence_id in ranked_ids]
        expected_parents = _expand_parents(ranked)
        if trace.get("ranked_parents") != expected_parents:
            raise ValueError("candidate trace parent expansion mismatch")
        answer = {
            "abstained": trace.get("abstained"),
            "reason": trace.get("abstention_reason"),
        }
        if type(answer["abstained"]) is not bool:
            raise ValueError("candidate trace abstention is invalid")
        if require_normalized_confidence:
            expected_abstained = not ranked_evidence or ranked_evidence[0]["score"] < 0.54
            if answer["abstained"] is not expected_abstained:
                raise ValueError("candidate trace abstention differs from normalized confidence")
            expected_reason = "not-in-corpus" if expected_abstained else None
            if answer["reason"] != expected_reason:
                raise ValueError("candidate trace abstention reason differs from calibration")
        row = _evaluation_row(query, ranked, answer)
        if trace.get("abstention_contract_valid") is not row["abstention_contract_valid"]:
            raise ValueError("candidate trace abstention contract mismatch")
        rows.append(row)
    overall = _aggregate(rows)
    slices = {
        language: _aggregate([row for row in rows if row["language"] == language])
        for language in ("EN", "RU", "ZH")
    }
    slices["cross-language"] = _aggregate([row for row in rows if row["cross_language"]])
    _metrics_match(report["overall"], overall, "overall")
    _metrics_match(report["slices"], slices, "slices")
    macro = {metric: macro_average(slices, metric) for metric in EFFECTIVENESS_FIELDS}
    _metrics_match(report["macro_average"], macro, "macro_average")
    return overall, slices


def _baseline_policy_sha256(report: dict) -> str:
    return _sha256_json(
        {
            "acquisition_mode": report.get("acquisition_mode"),
            "artifact_kind": "retrieval-baseline-policy",
            "benchmark_contract_sha256": report.get("benchmark_contract_sha256"),
            "benchmark_runner_sha256": report.get("benchmark_runner_sha256"),
            "candidate": report.get("candidate"),
            "corpus_sha256": report.get("corpus_sha256"),
            "environment_provenance": report.get("methodology", {}).get(
                "environment_provenance"
            ),
            "lexical_configuration": report.get("methodology", {}).get(
                "lexical_configuration"
            ),
            "matrix_sha256": report.get("matrix_sha256"),
            "schema_version": 1,
            "thresholds_basis_points": {
                name: round(float(value) * 10_000)
                for name, value in (report.get("thresholds") or {}).items()
            },
            "vector_backend": report.get("vector_backend"),
        }
    )


def _verified_baseline_metrics(matrix: dict, report: dict, *, corpus_path: Path | str):
    baseline_target = {
        "embedding": {
            "dimensions": 384,
            "id": "BAAI/bge-small-en-v1.5",
            "revision": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
            "variant_id": "float32-384d",
        },
        "reranker": None,
    }
    environment = report.get("methodology", {}).get("environment_provenance")
    comparable_environment = dict(environment) if isinstance(environment, dict) else environment
    if isinstance(comparable_environment, dict):
        comparable_environment.pop("verified_lock", None)
    expected_environment = _environment_provenance("numpy-exact")
    expected_environment["runner_sha256"] = report.get("benchmark_runner_sha256")
    if matrix["selection"]["baseline"].get("policy_sha256") != _baseline_policy_sha256(
        report
    ):
        raise ValueError("bound baseline policy fingerprint mismatch")
    if (
        set(report) != REPORT_FIELDS
        or report.get("schema_version") != "retrieval-report/v2"
        or report.get("corpus_id") != "public-synthetic-retrieval-v2"
        or report.get("adapter_kind") != MODEL_MATRIX_ADAPTER_KIND
        or report.get("quality_claim") is not True
        or report.get("requested_mode") != MODEL_MATRIX_ADAPTER_KIND
        or report.get("candidate") != baseline_target
        or report.get("effective_mode") != MODEL_MATRIX_ADAPTER_KIND
        or report.get("fallback_reason") is not None
        or report.get("corpus_sha256") != matrix["benchmark_contract"]["corpus"]["sha256"]
        or report.get("benchmark_contract_sha256") != _sha256_json(matrix["benchmark_contract"])
        or report.get("acquisition_mode") != "offline-local-files-only"
        or report.get("vector_backend") != "numpy-exact"
        or report.get("release_evidence") is not False
        or report.get("thresholds") != THRESHOLDS
        or report.get("methodology", {}).get("lexical_configuration")
        != LEXICAL_CONFIGURATIONS["L4"]
        or comparable_environment != expected_environment
        or not isinstance(environment, dict)
        or not isinstance(environment.get("verified_lock"), dict)
    ):
        raise ValueError("bound baseline is not comparable retrieval-v2 BGE-small evidence")
    corpus = load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    overall, slices = _recompute_report_metrics(corpus, report)
    quality = round(
        sum(
            float(overall[name])
            for name in (
                "parent_recall_at_10",
                "all_required_evidence_recall_at_20",
                "ndcg_at_10",
                "mrr_at_10",
            )
        )
        / 4
        * 10_000
    )
    baseline = matrix["selection"]["baseline"]
    if baseline["overall_basis_points"] != quality or baseline[
        "parent_recall_at_10_basis_points"
    ] != round(float(overall["parent_recall_at_10"]) * 10_000):
        raise ValueError("selection baseline metrics do not match bound raw evidence")
    return overall, slices


def _select_lexical_winner(reports: Sequence[dict]) -> dict:
    by_id = {}
    provenance = None
    for report in reports:
        configuration = report.get("methodology", {}).get("lexical_configuration")
        lexical = configuration.get("id") if isinstance(configuration, dict) else None
        if lexical not in LEXICAL_CONFIGURATIONS or lexical in by_id:
            raise ValueError("lexical evidence must contain each L0 through L4 exactly once")
        if configuration != LEXICAL_CONFIGURATIONS[lexical]:
            raise ValueError("lexical evidence configuration differs from the frozen matrix")
        comparable = tuple(
            report.get(field)
            for field in (
                "schema_version",
                "corpus_sha256",
                "benchmark_contract_sha256",
                "benchmark_runner_sha256",
                "adapter_kind",
                "effective_mode",
            )
        )
        if provenance is None:
            provenance = comparable
        elif comparable != provenance:
            raise ValueError("lexical ablation evidence is not comparable")
        overall = report.get("overall", {})
        values = [
            overall.get(name)
            for name in (
                "parent_recall_at_10",
                "all_required_evidence_recall_at_20",
                "ndcg_at_10",
                "mrr_at_10",
            )
        ]
        latency = report.get("measurements", {}).get("warm_latency_p95_ms")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values) or not (
            isinstance(latency, (int, float)) and math.isfinite(latency) and latency >= 0
        ):
            raise ValueError("lexical ablation evidence is incomplete or nonfinite")
        by_id[lexical] = {
            "id": lexical,
            "quality": sum(float(value) for value in values) / len(values),
            "warm_latency_p95_ms": float(latency),
        }
    if set(by_id) != set(LEXICAL_CONFIGURATIONS):
        raise ValueError("lexical evidence requires the complete L0 through L4 ablation set")
    return min(
        by_id.values(),
        key=lambda item: (-item["quality"], item["warm_latency_p95_ms"], item["id"]),
    )


def _recompute_reranker_depth_metrics(corpus: dict, report: dict) -> dict[str, tuple[dict, dict]]:
    if report["reranker"] is None:
        return {}
    recomputed = {}
    candidates = {item.evidence_id: item for item in build_candidates(corpus)}
    for depth in report["reranker"]["depths"]:
        depth_key = str(depth)
        depth_traces = []
        inference_latencies = []
        query_latencies = []
        for trace in report["traces"]:
            depth_evidence = trace.get("reranker", {}).get("depths", {}).get(depth_key)
            if not isinstance(depth_evidence, dict) or not isinstance(
                depth_evidence.get("ranked_evidence"), list
            ):
                raise ValueError("reranker trace lacks complete depth rankings")
            duration = depth_evidence.get("duration_ms")
            query_latency = depth_evidence.get("query_latency_ms")
            if not all(
                isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                for value in (duration, query_latency)
            ):
                raise ValueError("reranker depth trace lacks finite timing")
            inference_latencies.append(float(duration))
            query_latencies.append(float(query_latency))
            copied = dict(trace)
            copied["ranked_evidence"] = depth_evidence["ranked_evidence"]
            copied["abstained"] = depth_evidence.get("abstained")
            copied["abstention_reason"] = depth_evidence.get("abstention_reason")
            query = next(
                item for item in corpus["queries"] if item["query_id"] == copied["query_id"]
            )
            copied["abstention_contract_valid"] = (
                not copied["abstained"]
                if query["answerability"] == "answerable"
                else copied["abstained"]
                and copied["abstention_reason"] == query["allowed_abstention_reason"]
            )
            ranked = [
                ScoredCandidate(candidates[item["evidence_id"]], 0.0)
                for item in copied["ranked_evidence"]
                if item["evidence_id"] in candidates
            ]
            copied["ranked_parents"] = _expand_parents(ranked)
            depth_traces.append(copied)
        claimed = report["reranker"]["depth_metrics"][depth_key]
        warm_p95 = _resource_measurements(
            query_latencies,
            build_time_ms=0,
            chunk_count=0,
            index_size_bytes=0,
            peak_rss_bytes=None,
            peak_rss_status="shared-run-level",
        )["warm_latency_p95_ms"]
        _metrics_match(claimed["inference_latencies_ms"], inference_latencies, "reranker timing")
        _metrics_match(claimed["duration_ms"], sum(inference_latencies), "reranker duration")
        _metrics_match(claimed["warm_latency_p95_ms"], warm_p95, "reranker warm p95")
        synthetic = dict(
            report,
            traces=depth_traces,
            overall=claimed["overall"],
            slices=claimed["slices"],
            macro_average={
                metric: macro_average(claimed["slices"], metric)
                for metric in EFFECTIVENESS_FIELDS
            },
        )
        recomputed[depth_key] = _recompute_report_metrics(corpus, synthetic)
    return recomputed


def _selection_report_gates(matrix: dict, report: dict, embedding: dict, reranker: dict | None):
    selection = matrix["selection"]
    overall = report["overall"]
    slices = report["slices"]
    quality_metrics = (
        "parent_recall_at_10",
        "all_required_evidence_recall_at_20",
        "ndcg_at_10",
        "mrr_at_10",
    )
    quality_values = [float(overall[name]) for name in quality_metrics]
    language_values = [
        float(slices[language][name])
        for language in ("EN", "RU", "ZH")
        for name in quality_metrics
    ]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in quality_values + language_values):
        raise ValueError("raw report quality metrics must be finite rates")
    false_answer_values = [
        overall["no_answer_false_answer_rate"],
        *(slices[language]["no_answer_false_answer_rate"] for language in ("EN", "RU", "ZH")),
    ]
    if not all(
        isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1
        for value in false_answer_values
    ):
        raise ValueError("raw report false-answer metrics must be finite rates")
    quality = sum(quality_values) / len(quality_values)
    quality_basis_points = round(quality * 10_000)
    per_language = {
        language: all(
            float(slices[language][metric])
            >= threshold - THRESHOLDS["max_language_gate_gap"]
            for metric, threshold in (
                ("parent_recall_at_10", THRESHOLDS["parent_recall_at_10"]),
                (
                    "all_required_evidence_recall_at_20",
                    THRESHOLDS["all_required_evidence_recall_at_20"],
                ),
                ("ndcg_at_10", THRESHOLDS["ndcg_at_10"]),
                ("mrr_at_10", THRESHOLDS["mrr_at_10"]),
            )
        )
        and slices[language]["no_answer_false_answer_rate"]
        <= THRESHOLDS["no_answer_false_answer_rate"]
        for language in ("EN", "RU", "ZH")
    }
    measurements = report["measurements"]
    p95 = measurements["warm_latency_p95_ms"]
    rss = measurements["peak_rss_bytes"]
    index_bytes = measurements["index_size_bytes"]
    if not all(
        isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        for value in (p95, rss, index_bytes)
    ):
        raise ValueError("raw report has incomplete or nonfinite resource measurements")
    baseline = selection["baseline"]
    material = selection["material_improvement"]
    parent_ok = round(float(overall["parent_recall_at_10"]) * 10_000) >= baseline[
        "parent_recall_at_10_basis_points"
    ]
    material_ok = quality_basis_points >= (
        baseline["overall_basis_points"] + material["minimum_absolute_gain_basis_points"]
    )
    shipping = all(
        item is None
        or (
            item["shipping_eligible"]
            and not item["exclusion_reasons"]
            and item["license"] in {"Apache-2.0", "MIT"}
            and item["trust_remote_code"] is False
        )
        for item in (embedding, reranker)
    )
    false_answer_ok = (
        overall["no_answer_false_answer_rate"]
        <= THRESHOLDS["no_answer_false_answer_rate"]
    )
    required = {
        "every_language_gate": all(per_language.values()) and false_answer_ok,
        "latency": p95 <= selection["limits"]["warm_p95_ms"],
        "license": shipping,
        "no_parent_recall_at_10_regression": parent_ok,
        "ram": rss <= selection["limits"]["peak_rss_bytes"],
    }
    gates = {
        "material_improvement": material_ok,
        "no_parent_recall_at_10_regression": parent_ok,
        "overall": all(required.values()) and material_ok,
        "per_language": per_language,
        "required": required,
        "shipping_eligible": shipping,
    }
    objective = {
        "index_bytes": int(index_bytes),
        "overall": quality_basis_points,
        "peak_rss_bytes": int(rss),
        "warm_p95_ms": math.ceil(p95),
    }
    return gates, objective


def _objective_dominates(left: dict, right: dict, objectives: dict) -> bool:
    weak = []
    strict = []
    for name, direction in objectives.items():
        if direction == "maximize":
            weak.append(left[name] >= right[name])
            strict.append(left[name] > right[name])
        else:
            weak.append(left[name] <= right[name])
            strict.append(left[name] < right[name])
    return all(weak) and any(strict)


def _aggregate_reports(
    raw_report_paths: Sequence[Path | str],
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
    _lexical_results: Sequence[_WorkerPayload] | None = None,
    _write_analysis: bool = True,
) -> dict:
    matrix_raw = read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix")
    matrix = json.loads(matrix_raw)
    first = matrix["embeddings"][0]
    validated = load_model_selection(
        matrix_path,
        corpus_path,
        model_id=first["id"],
        variant_id=first["variants"][0]["variant_id"],
    )
    try:
        parsed_time = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("aggregation measured_at must be an ISO UTC timestamp") from exc
    if not measured_at.endswith("Z") or parsed_time.utcoffset() is None:
        raise ValueError("aggregation measured_at must be an ISO UTC timestamp")

    repo = Path(repo_root).resolve(strict=True)
    requested_output = Path(output_path)
    if requested_output.is_symlink():
        raise ValueError("selection output must not be a symlink")
    if not requested_output.is_absolute():
        requested_output = repo / requested_output
    try:
        relative = requested_output.relative_to(repo)
    except ValueError as exc:
        raise ValueError("selection output must be under benchmark/results") from exc
    normalized = PurePosixPath(relative.as_posix())
    if (
        normalized.parts[:2] != ("benchmark", "results")
        or normalized.suffix != ".json"
        or any(part in {"", ".", ".."} for part in normalized.parts)
        or any(marker in normalized.as_posix().casefold() for marker in ("private", "personal"))
    ):
        raise ValueError("selection output must be a normalized JSON path under benchmark/results")
    output = repo / Path(*normalized.parts)
    if output.exists() or output.is_symlink():
        raise ValueError("selection output already exists")

    baseline = matrix["selection"]["baseline"]
    baseline_path = repo / Path(*PurePosixPath(baseline["raw_report_path"]).parts)
    baseline_raw = read_stable_bytes(baseline_path, MAX_CORPUS_BYTES, label="baseline raw report")
    if hashlib.sha256(baseline_raw).hexdigest() != baseline["raw_report_sha256"]:
        raise ValueError("bound baseline raw report SHA256 mismatch")
    try:
        baseline_report = json.loads(baseline_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("bound baseline raw report is invalid") from exc
    if baseline_raw != _canonical_report_bytes(baseline_report):
        raise ValueError("bound baseline raw report must use canonical JSON bytes")
    _verified_baseline_metrics(matrix, baseline_report, corpus_path=corpus_path)
    corpus = load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    expected = {_candidate_key(spec): spec for spec in required_candidate_specs(matrix)}
    reports = {}
    report_hashes = {}
    report_bytes = {}
    recomputed_metrics = {}
    recomputed_depth_metrics = {}
    common_environment = None
    contract_hash = _sha256_json(matrix["benchmark_contract"])
    runner_hash = _sha256_file(Path(__file__))
    for raw_path in raw_report_paths:
        raw = read_stable_bytes(Path(raw_path), MAX_CORPUS_BYTES, label="candidate raw report")
        try:
            report = json.loads(
                raw,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"nonfinite JSON constant: {value}")
                ),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid candidate raw report: {exc}") from exc
        _require_exact_keys(report, REPORT_FIELDS, "candidate raw report")
        if raw != _canonical_report_bytes(report):
            raise ValueError("candidate raw report must use canonical JSON bytes")
        if (
            report["schema_version"] != "retrieval-report/v2"
            or report["corpus_id"] != "public-synthetic-retrieval-v2"
            or report["adapter_kind"] != MODEL_MATRIX_ADAPTER_KIND
            or report["release_evidence"] is not False
            or report["effective_mode"] != MODEL_MATRIX_ADAPTER_KIND
            or report["fallback_reason"] is not None
            or report["matrix_sha256"] != validated.matrix_sha256
            or report["corpus_sha256"] != validated.corpus_sha256
            or report["benchmark_contract_sha256"] != contract_hash
            or report["benchmark_runner_sha256"] != runner_hash
            or report["acquisition_mode"] != "offline-local-files-only"
            or report["thresholds"] != THRESHOLDS
            or report["vector_backend"] == "usearch-hnsw"
        ):
            raise ValueError("candidate raw report provenance is incomplete")
        environment = report["methodology"].get("environment_provenance")
        expected_environment = _environment_provenance(report["vector_backend"])
        comparable_environment = dict(environment) if isinstance(environment, dict) else environment
        if isinstance(comparable_environment, dict):
            comparable_environment.pop("verified_lock", None)
        if comparable_environment != expected_environment:
            raise ValueError("candidate environment provenance or lock hash mismatch")
        if common_environment is None:
            common_environment = environment
        elif environment != common_environment:
            raise ValueError("candidate environment provenance is inconsistent")
        key = _candidate_key(report["candidate"])
        if key not in expected or key in reports:
            raise ValueError("candidate raw report is unknown or duplicated")
        recomputed_metrics[key] = _recompute_report_metrics(corpus, report)
        recomputed_depth_metrics[key] = _recompute_reranker_depth_metrics(corpus, report)
        reports[key] = report
        report_hashes[key] = hashlib.sha256(raw).hexdigest()
        report_bytes[key] = raw
    if set(reports) != set(expected):
        raise ValueError("aggregation requires the complete candidate report set")

    evaluated = []
    for key in sorted(reports):
        report = dict(reports[key])
        report["overall"], report["slices"] = recomputed_metrics[key]
        embedding, reranker = _candidate_policy(matrix, report["candidate"])
        report_variants = [(report["candidate"], report)]
        if reranker is not None:
            reranker_report = report["reranker"]
            target = report["candidate"]["reranker"]
            if (
                not isinstance(reranker_report, dict)
                or reranker_report.get("model_id") != target["id"]
                or reranker_report.get("revision") != target["revision"]
                or reranker_report.get("variant_id") != target["variant_id"]
                or reranker_report.get("depths") != matrix["benchmark_contract"]["reranker_depths"]
                or set(reranker_report.get("depth_metrics", {})) != {"10", "20", "50"}
            ):
                raise ValueError("reranker raw report lacks complete comparable depth evidence")
            report_variants = []
            for depth in matrix["benchmark_contract"]["reranker_depths"]:
                depth_metrics = reranker_report["depth_metrics"][str(depth)]
                derived = dict(report)
                derived["overall"], derived["slices"] = recomputed_depth_metrics[key][str(depth)]
                derived["measurements"] = dict(report["measurements"])
                if "warm_latency_p95_ms" in depth_metrics:
                    derived["measurements"]["warm_latency_p95_ms"] = depth_metrics[
                        "warm_latency_p95_ms"
                    ]
                candidate = dict(report["candidate"])
                candidate["rerank_depth"] = depth
                report_variants.append((candidate, derived))
        elif report["reranker"] is not None:
            raise ValueError("embedding-only report contains unexpected reranker evidence")
        for candidate, evaluated_report in report_variants:
            try:
                gates, objective = _selection_report_gates(
                    matrix, evaluated_report, embedding, reranker
                )
            except (KeyError, TypeError, OverflowError, ValueError) as exc:
                raise ValueError(f"candidate raw report metrics are invalid: {exc}") from exc
            evaluated.append(
                {
                    "candidate": candidate,
                    "gates": gates,
                    "objective_values": objective,
                    "raw_report_sha256": report_hashes[key],
                }
            )
    passing = [item for item in evaluated if item["gates"]["overall"]]
    frontier = [
        item
        for item in passing
        if not any(
            other is not item
            and _objective_dominates(
                other["objective_values"],
                item["objective_values"],
                matrix["selection"]["pareto_objectives"],
            )
            for other in passing
        )
    ]
    selected = (
        min(
            frontier,
            key=lambda item: (
                -item["objective_values"]["overall"],
                item["objective_values"]["warm_p95_ms"],
                item["objective_values"]["peak_rss_bytes"],
                item["objective_values"]["index_bytes"],
                _candidate_key(item["candidate"]),
            ),
        )
        if frontier
        else None
    )
    artifact = {
        "schema_version": "retrieval-comparison/v1",
        "quality_claim": False,
        "release_evidence": False,
        "measured_at": measured_at,
        "matrix_sha256": validated.matrix_sha256,
        "matrix_policy_sha256": matrix_policy_fingerprint(matrix),
        "corpus_sha256": validated.corpus_sha256,
        "benchmark_contract_sha256": contract_hash,
        "benchmark_runner_sha256": runner_hash,
        "baseline": baseline,
        "candidate_reports": [
            {
                "candidate": reports[key]["candidate"],
                "raw_report_sha256": report_hashes[key],
                "retained_path": (
                    f"benchmark/results/reports/{report_hashes[key]}.json"
                ),
            }
            for key in sorted(reports)
        ]
        + [
            {
                "lexical_configuration": item.report["methodology"]["lexical_configuration"][
                    "id"
                ],
                "raw_report_sha256": hashlib.sha256(item.canonical_bytes).hexdigest(),
                "retained_path": (
                    "benchmark/results/reports/"
                    f"{hashlib.sha256(item.canonical_bytes).hexdigest()}.json"
                ),
            }
            for item in (_lexical_results or ())
        ],
        "pareto": frontier,
        "selected": selected["candidate"] if selected is not None else None,
        "gates": (
            selected["gates"]
            if selected is not None
            else {"outcome": "no-winner", "fallback": "current-bm25"}
        ),
        "measurements": selected["objective_values"] if selected is not None else None,
    }
    if set(artifact) != set(matrix["selection"]["aggregation_evidence_contract"]["required_fields"]):
        raise ValueError("selection artifact does not match matrix evidence contract")
    if not _write_analysis:
        return artifact
    serialized = (canonical_json_bytes(artifact) + b"\n").decode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = output.parent.resolve(strict=True)
    if resolved_parent != repo and repo not in resolved_parent.parents:
        raise ValueError("selection output parent escaped benchmark/results")
    current = output.parent
    while current != repo:
        if _is_reparse_point(current):
            raise ValueError("selection output path must not contain symlinks or reparse points")
        current = current.parent
    output = resolved_parent / output.name
    retained_root = repo / "benchmark" / "results" / "reports"
    retained_root.mkdir(parents=True, exist_ok=True)
    resolved_retained_root = retained_root.resolve(strict=True)
    if resolved_retained_root != retained_root or _is_reparse_point(retained_root):
        raise ValueError("retained report path must not contain symlinks or reparse points")

    created_reports = []
    try:
        for key in sorted(reports):
            retained = retained_root / f"{report_hashes[key]}.json"
            identity = _write_new_bytes(retained, report_bytes[key])
            created_reports.append((retained, identity))
        for item in _lexical_results or ():
            digest = hashlib.sha256(item.canonical_bytes).hexdigest()
            retained = retained_root / f"{digest}.json"
            identity = _write_new_bytes(retained, item.canonical_bytes)
            created_reports.append((retained, identity))
        _write_new_output(output, serialized)
    except BaseException:
        for retained, identity in reversed(created_reports):
            with contextlib.suppress(FileNotFoundError):
                if os.path.samestat(identity, retained.stat(follow_symlinks=False)):
                    retained.unlink()
        raise
    return artifact


def aggregate_selection(
    raw_report_paths: Sequence[Path | str],
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
) -> dict:
    """Compare report files without producing release or selection evidence."""
    return _aggregate_reports(
        raw_report_paths,
        matrix_path=matrix_path,
        corpus_path=corpus_path,
        repo_root=repo_root,
        output_path=output_path,
        measured_at=measured_at,
    )


def _orchestrate_selection_impl(
    *,
    matrix_path: Path | str,
    corpus_path: Path | str,
    repo_root: Path | str,
    output_path: Path | str,
    measured_at: str,
    cache_root: Path | str | None = None,
    deadline_seconds: float | None = None,
    _execution_consumer=None,
) -> dict:
    if _execution_consumer is None:
        raise TypeError("authoritative orchestration requires its bound execution consumer")
    matrix = json.loads(read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix"))
    specs = required_candidate_specs(matrix)
    if cache_root is None or deadline_seconds is None:
        raise ValueError("authoritative orchestration requires cache root and deadline")
    requested_cache = Path(cache_root).expanduser()
    if requested_cache.exists() and _is_reparse_point(requested_cache):
        raise ValueError("model cache root must not be a symlink or reparse point")
    requested_cache.mkdir(parents=True, exist_ok=True)
    cache_root = _validate_cache_root(requested_cache).resolve(strict=True)
    if _is_reparse_point(cache_root):
        raise ValueError("model cache root must not be a symlink or reparse point")
    lexical_results = []
    execution_bound = True
    for argv in _lexical_ablation_worker_arguments(cache_root, deadline_seconds):
        payload = _run_bounded_model_worker(argv, deadline_seconds=deadline_seconds)
        if not isinstance(payload, _WorkerPayload):
            raise TypeError("lexical worker transport returned an invalid payload")
        _validate_lexical_worker_payload(payload, matrix=matrix, corpus_path=corpus_path)
        execution_bound &= _execution_consumer(payload)
        lexical_results.append(payload)
    lexical_config = _select_lexical_winner([item.report for item in lexical_results])["id"]
    candidate_payloads = []
    for spec in specs:
        argv = _candidate_worker_arguments(
            spec, cache_root, deadline_seconds, lexical_config=lexical_config
        )
        payload = _run_bounded_model_worker(argv, deadline_seconds=deadline_seconds)
        if not isinstance(payload, _WorkerPayload):
            raise TypeError("authoritative worker transport returned an invalid payload")
        if payload.report.get("effective_mode") != MODEL_MATRIX_ADAPTER_KIND:
            raise ValueError(
                f"authoritative candidate {_candidate_key(spec)} degraded: "
                f"fallback_reason={payload.report.get('fallback_reason')!s}"
            )
        if payload.report.get("methodology", {}).get("lexical_configuration", {}).get(
            "id"
        ) != lexical_config:
            raise ValueError("candidate report does not use the frozen lexical winner")
        execution_bound &= _execution_consumer(payload)
        candidate_payloads.append(payload)
        _validate_worker_payload(payload.report, payload.canonical_bytes)
        if payload.report.get("candidate") != spec:
            raise ValueError("worker result does not match requested matrix candidate")
    with tempfile.TemporaryDirectory(prefix="llm-wiki-authoritative-selection-") as temporary:
        paths = []
        for index, result in enumerate(candidate_payloads):
            path = Path(temporary) / f"candidate-{index}.json"
            _write_new_bytes(path, result.canonical_bytes)
            paths.append(path)
        artifact = _aggregate_reports(
            paths,
            matrix_path=matrix_path,
            corpus_path=corpus_path,
            repo_root=repo_root,
            output_path=output_path,
            measured_at=measured_at,
            _lexical_results=lexical_results,
            _write_analysis=not execution_bound,
        )
    if not execution_bound:
        return artifact

    artifact["schema_version"] = "retrieval-selection/v1"
    artifact["quality_claim"] = True
    artifact["release_evidence"] = True
    serialized = canonical_json_bytes(artifact) + b"\n"
    repo = Path(repo_root).resolve(strict=True)
    requested_output = Path(output_path)
    if not requested_output.is_absolute():
        requested_output = repo / requested_output
    output = requested_output.parent.resolve() / requested_output.name
    if output.exists() or output.is_symlink():
        raise ValueError("selection output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = output.parent.resolve(strict=True)
    if resolved_parent != repo and repo not in resolved_parent.parents:
        raise ValueError("selection output parent escaped benchmark/results")
    current = output.parent
    while current != repo:
        if _is_reparse_point(current):
            raise ValueError("selection output path must not contain symlinks or reparse points")
        current = current.parent
    retained_root = repo / "benchmark" / "results" / "reports"
    retained_root.mkdir(parents=True, exist_ok=True)
    resolved_retained_root = retained_root.resolve(strict=True)
    if resolved_retained_root != retained_root or _is_reparse_point(retained_root):
        raise ValueError("retained report path must not contain symlinks or reparse points")
    created_reports = []
    try:
        for payload in [*candidate_payloads, *lexical_results]:
            digest = hashlib.sha256(payload.canonical_bytes).hexdigest()
            retained = retained_root / f"{digest}.json"
            identity = _write_new_bytes(retained, payload.canonical_bytes)
            created_reports.append((retained, identity))
        _write_new_output(resolved_parent / output.name, serialized.decode("utf-8"))
    except BaseException:
        for retained, identity in reversed(created_reports):
            with contextlib.suppress(FileNotFoundError):
                if os.path.samestat(identity, retained.stat(follow_symlinks=False)):
                    retained.unlink()
        raise
    return artifact


def _lexical_ablation_worker_arguments(cache_root: Path, deadline_seconds: float) -> list[list[str]]:
    return [
        [
            "--adapter",
            ADAPTER_KIND,
            "--cache-root",
            str(cache_root),
            "--lexical-config",
            level,
            "--deadline-seconds",
            str(deadline_seconds),
        ]
        for level in LEXICAL_CONFIGURATIONS
    ]


def _validate_lexical_worker_payload(
    payload: _WorkerPayload, *, matrix: dict, corpus_path: Path | str
) -> None:
    report = payload.report
    if payload.canonical_bytes != _canonical_report_bytes(report):
        raise ValueError("lexical worker report is not canonical")
    _require_exact_keys(report, REPORT_FIELDS, "lexical worker report")
    lexical = report.get("methodology", {}).get("lexical_configuration", {}).get("id")
    if (
        lexical not in LEXICAL_CONFIGURATIONS
        or report["methodology"]["lexical_configuration"] != LEXICAL_CONFIGURATIONS[lexical]
        or report["quality_claim"] is not False
        or report["release_evidence"] is not False
        or report["candidate"] is not None
        or report["model_id"] is not None
        or report["fallback_reason"] is not None
        or report["corpus_sha256"] != matrix["benchmark_contract"]["corpus"]["sha256"]
        or report["matrix_sha256"]
        != hashlib.sha256(canonical_json_bytes(matrix) + b"\n").hexdigest()
        or report["benchmark_contract_sha256"] != _sha256_json(matrix["benchmark_contract"])
        or report["benchmark_runner_sha256"] != _sha256_file(Path(__file__))
        or report["methodology"].get("environment_provenance") != _environment_provenance(None)
    ):
        raise ValueError("lexical worker provenance is incomplete or incomparable")
    corpus = load_corpus(corpus_path, Path(corpus_path).with_name(DEFAULT_SCHEMA.name))
    _recompute_report_metrics(
        corpus,
        report,
        require_complete_rankings=False,
        require_normalized_confidence=False,
    )


def _candidate_worker_arguments(
    spec: dict,
    cache_root: Path,
    deadline_seconds: float,
    *,
    lexical_config: str,
) -> list[str]:
    argv = [
        "--adapter",
        MODEL_MATRIX_ADAPTER_KIND,
        "--model-id",
        spec["embedding"]["id"],
        "--variant-id",
        spec["embedding"]["variant_id"],
        "--cache-root",
        str(cache_root),
        "--lexical-config",
        lexical_config,
        "--vector-backend",
        "numpy-exact",
        "--deadline-seconds",
        str(deadline_seconds),
    ]
    if spec["reranker"] is not None:
        argv.extend(("--reranker-id", spec["reranker"]["id"]))
    return argv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen retrieval-v2 orchestration benchmark (the run_benchmark.py default); "
            "use run_benchmark.py --legacy-only for the legacy slice"
        )
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--json", action="store_true", help="Print the complete JSON report")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report")
    parser.add_argument("--cache-root", type=Path, help="Explicit isolated benchmark cache directory")
    parser.add_argument(
        "--adapter",
        choices=(
            ADAPTER_KIND,
            MODEL_MATRIX_ADAPTER_KIND,
            SELECTION_AGGREGATION_ADAPTER_KIND,
            AUTHORITATIVE_SELECTION_ADAPTER_KIND,
        ),
        default=ADAPTER_KIND,
    )
    parser.add_argument(
        "--lexical-config",
        choices=tuple(LEXICAL_CONFIGURATIONS),
        help="Run one explicit BM25-only lexical ablation (L0 through L4)",
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--model-id")
    parser.add_argument("--variant-id")
    parser.add_argument("--reranker-id")
    parser.add_argument(
        "--rerank-depth", type=int, choices=(10, 20, 50), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--vector-backend", choices=("numpy-exact", "usearch-exact", "usearch-hnsw")
    )
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--internal-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--raw-report", action="append", type=Path)
    parser.add_argument("--selection-output", type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--measured-at")
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--semantic", action="store_true", help="Reserved for Task 10 model runs")
    parser.add_argument("--report", action="store_true", help="Reserved for Task 10 report publication")
    return parser


def _validate_cli_args(args: argparse.Namespace) -> None:
    if args.adapter == AUTHORITATIVE_SELECTION_ADAPTER_KIND:
        if (
            args.cache_root is None
            or args.selection_output is None
            or args.measured_at is None
            or args.deadline_seconds is None
            or not math.isfinite(args.deadline_seconds)
            or args.deadline_seconds <= 0
        ):
            raise ValueError(
                "authoritative-selection requires cache root, selection output, measured-at, and deadline"
            )
        if args.raw_report or any(
            value is not None
            for value in (args.model_id, args.variant_id, args.reranker_id, args.output)
        ) or args.allow_download:
            raise ValueError("authoritative-selection derives candidates from the closed matrix")
    elif args.adapter == SELECTION_AGGREGATION_ADAPTER_KIND:
        if not args.raw_report or args.selection_output is None or args.measured_at is None:
            raise ValueError(
                "selection-aggregation requires explicit --raw-report, --selection-output, and --measured-at"
            )
        if any(
            value is not None
            for value in (
                args.cache_root,
                args.model_id,
                args.variant_id,
                args.reranker_id,
                args.rerank_depth,
                args.vector_backend,
                args.lexical_config,
                args.output,
            )
        ) or args.allow_download:
            raise ValueError("selection-aggregation does not accept model-run options")
    elif args.adapter == MODEL_MATRIX_ADAPTER_KIND:
        required = {
            "--model-id": args.model_id,
            "--variant-id": args.variant_id,
            "--cache-root": args.cache_root,
            "--lexical-config": args.lexical_config,
            "--vector-backend": args.vector_backend,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "model-matrix adapter requires explicit " + ", ".join(missing)
            )
        if args.deadline_seconds is None or not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0:
            raise ValueError("model-matrix adapter requires positive --deadline-seconds")
        if args.rerank_depth is not None:
            raise ValueError("reranker evidence automatically runs depths 10, 20, and 50 together")
    elif any(
        value is not None
        for value in (
            args.model_id,
            args.variant_id,
            args.reranker_id,
            args.rerank_depth,
            args.vector_backend,
        )
    ) or args.allow_download:
        raise ValueError("real-model options require --adapter model-matrix")


def _worker_arguments(argv: Sequence[str], output: Path) -> list[str]:
    cleaned = []
    skip = False
    for index, value in enumerate(argv):
        if skip:
            skip = False
            continue
        if value == "--output":
            skip = True
            continue
        if value == "--internal-worker":
            continue
        if value == "--json":
            continue
        cleaned.append(value)
    return [*cleaned, "--output", str(output), "--internal-worker"]


def _validate_worker_payload(report: dict, raw: bytes) -> None:
    if raw != _canonical_report_bytes(report):
        raise ValueError("worker report is not canonical")
    _require_exact_keys(report, REPORT_FIELDS, "worker report")
    if report["quality_claim"] is not False or report["release_evidence"] is not False:
        raise ValueError("worker payload attempted to self-attest")
    if report["effective_mode"] != MODEL_MATRIX_ADAPTER_KIND:
        raise ValueError("degraded worker payload cannot become quality evidence")
    reranker_target = report.get("candidate", {}).get("reranker")
    selection = load_model_selection(
        DEFAULT_MATRIX,
        DEFAULT_CORPUS,
        model_id=report.get("model_id"),
        variant_id=report.get("variant_id"),
        reranker_id=reranker_target.get("id") if isinstance(reranker_target, dict) else None,
    )
    expected_candidate = {
        "embedding": _matrix_target(selection.embedding, selection.variant),
        "reranker": (
            _matrix_target(selection.reranker, selection.reranker_variant)
            if selection.reranker is not None
            else None
        ),
    }
    if (
        report["candidate"] != expected_candidate
        or report["matrix_sha256"] != selection.matrix_sha256
        or report["corpus_sha256"] != selection.corpus_sha256
        or report["benchmark_contract_sha256"]
        != _sha256_json(selection.matrix["benchmark_contract"])
        or report["benchmark_runner_sha256"] != _sha256_file(Path(__file__))
        or report["acquisition_mode"] != "offline-local-files-only"
        or report["fallback_reason"] is not None
        or report["thresholds"] != THRESHOLDS
    ):
        raise ValueError("worker payload provenance is incomplete")
    environment = report["methodology"].get("environment_provenance")
    if environment != _environment_provenance(report["vector_backend"]):
        raise ValueError("worker payload environment provenance mismatch")
    corpus = load_corpus(DEFAULT_CORPUS, DEFAULT_SCHEMA)
    _recompute_report_metrics(corpus, report)
    _recompute_reranker_depth_metrics(corpus, report)
    lexical = report["methodology"].get("lexical_configuration", {}).get("id")
    _verify_locked_environment(report["vector_backend"], lexical_config=lexical)


def _run_bounded_model_worker_impl(
    argv: Sequence[str], *, deadline_seconds: float
) -> _WorkerPayload | dict:
    with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-worker-") as temporary:
        report_path = Path(temporary) / "report.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            *_worker_arguments(argv, report_path),
        ]
        try:
            completed = _run_process_tree(command, timeout=deadline_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                "real benchmark worker failed: "
                f"timeout_seconds={deadline_seconds}; termination=deadline-exceeded; {exc}"
            ) from exc
        if completed.returncode not in {0, 2} or not report_path.is_file():
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            termination = (
                f"signal-{abs(completed.returncode)}"
                if completed.returncode < 0
                else "exit-code"
            )
            raise ValueError(
                "real benchmark worker failed: "
                f"returncode={completed.returncode}; timeout_seconds={deadline_seconds}; "
                f"termination={termination}; stderr={error or 'empty'}"
            )
        raw = read_stable_bytes(report_path, MAX_CORPUS_BYTES, label="worker report")
        report = json.loads(raw)
        if report.get("artifact_kind") == "model-acquisition-receipt":
            if raw != _canonical_report_bytes(report) or report.get("quality_claim") is not False:
                raise ValueError("model acquisition receipt is invalid")
            return report
        if raw != _canonical_report_bytes(report):
            raise ValueError("worker report is not canonical")
        if report.get("quality_claim") is not False or report.get("release_evidence") is not False:
            raise ValueError("worker transport received a self-attested payload")
        return _WorkerPayload(report, raw)


def _make_execution_bound_worker(worker, process_runner):
    registry = {}

    def run(argv: Sequence[str], *, deadline_seconds: float):
        execution_bound = _run_process_tree is process_runner
        result = worker(argv, deadline_seconds=deadline_seconds)
        if execution_bound and isinstance(result, _WorkerPayload):
            registry[id(result)] = (
                result,
                hashlib.sha256(result.canonical_bytes).digest(),
            )
        return result

    def consume(payload: _WorkerPayload) -> bool:
        registered = registry.pop(id(payload), None)
        return (
            registered is not None
            and registered[0] is payload
            and registered[1] == hashlib.sha256(payload.canonical_bytes).digest()
        )

    return run, consume


_run_bounded_model_worker, _consume_execution_bound_payload = _make_execution_bound_worker(
    _run_bounded_model_worker_impl, _run_process_tree
)
del _make_execution_bound_worker


def _bind_orchestrator(implementation, execution_consumer):
    def orchestrate_selection(
        *,
        matrix_path: Path | str,
        corpus_path: Path | str,
        repo_root: Path | str,
        output_path: Path | str,
        measured_at: str,
        cache_root: Path | str | None = None,
        deadline_seconds: float | None = None,
    ) -> dict:
        return implementation(
            matrix_path=matrix_path,
            corpus_path=corpus_path,
            repo_root=repo_root,
            output_path=output_path,
            measured_at=measured_at,
            cache_root=cache_root,
            deadline_seconds=deadline_seconds,
            _execution_consumer=execution_consumer,
        )

    return orchestrate_selection


orchestrate_selection = _bind_orchestrator(
    _orchestrate_selection_impl, _consume_execution_bound_payload
)
del _bind_orchestrator
del _orchestrate_selection_impl


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_cli_args(args)
        if args.semantic or args.report:
            raise ValueError("use --adapter model-matrix instead of legacy --semantic/--report")
        if args.model:
            raise ValueError("use exact --model-id and --variant-id selectors")
        if args.adapter == AUTHORITATIVE_SELECTION_ADAPTER_KIND:
            artifact = orchestrate_selection(
                matrix_path=args.matrix,
                corpus_path=args.corpus,
                repo_root=args.repo_root,
                output_path=args.selection_output,
                measured_at=args.measured_at,
                cache_root=args.cache_root,
                deadline_seconds=args.deadline_seconds,
            )
            serialized = json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ) + "\n"
            if args.json:
                print(serialized, end="")
            else:
                print(
                    "retrieval-v2 authoritative selection complete; "
                    f"release_evidence={str(artifact['release_evidence']).lower()}"
                )
            return 0
        if args.adapter == SELECTION_AGGREGATION_ADAPTER_KIND:
            artifact = aggregate_selection(
                args.raw_report,
                matrix_path=args.matrix,
                corpus_path=args.corpus,
                repo_root=args.repo_root,
                output_path=args.selection_output,
                measured_at=args.measured_at,
            )
            serialized = json.dumps(
                artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ) + "\n"
            if args.json:
                print(serialized, end="")
            else:
                print("retrieval-v2 report comparison complete; release_evidence=false")
            return 0
        if args.adapter == MODEL_MATRIX_ADAPTER_KIND and args.allow_download:
            if not args.internal_worker:
                source_argv = list(argv) if argv is not None else sys.argv[1:]
                receipt = _run_bounded_model_worker(
                    source_argv, deadline_seconds=args.deadline_seconds
                )
            else:
                selection = load_model_selection(
                    args.matrix,
                    args.corpus,
                    model_id=args.model_id,
                    variant_id=args.variant_id,
                    reranker_id=args.reranker_id,
                )
                receipt = prefetch_models(selection, cache_root=args.cache_root)
            serialized = _canonical_report_bytes(receipt).decode("utf-8")
            if args.output is not None:
                output = args.output.expanduser()
                if not output.is_absolute():
                    output = Path.cwd() / output
                if output.exists() or output.is_symlink():
                    raise ValueError("--output must not already exist")
                output.parent.mkdir(parents=True, exist_ok=True)
                _write_new_output(output.parent.resolve(strict=True) / output.name, serialized)
            if args.json:
                print(serialized, end="")
            else:
                print("model acquisition complete; quality_claim=false")
            return 0
        report = None
        if args.adapter == MODEL_MATRIX_ADAPTER_KIND and not args.internal_worker:
            source_argv = list(argv) if argv is not None else sys.argv[1:]
            payload = _run_bounded_model_worker(
                source_argv, deadline_seconds=args.deadline_seconds
            )
            if not isinstance(payload, _WorkerPayload):
                raise ValueError("benchmark worker did not return a worker payload")
            _consume_execution_bound_payload(payload)
            _validate_worker_payload(payload.report, payload.canonical_bytes)
            report = json.loads(json.dumps(payload.report))
            if args.output is None:
                report["gates"]["interpretation"] = "stdout-only-non-quality"
        corpus = load_corpus(args.corpus, args.schema)
        run_options = {
            "adapter": args.adapter,
            "corpus_path": args.corpus,
            "matrix_path": args.matrix,
            "model_id": args.model_id,
            "variant_id": args.variant_id,
            "reranker_id": args.reranker_id,
            "rerank_depth": args.rerank_depth,
            "vector_backend": args.vector_backend,
            "allow_download": args.allow_download,
            "lexical_config": args.lexical_config,
            "raw_output_written": args.output is not None,
        }
        if args.adapter == MODEL_MATRIX_ADAPTER_KIND and args.internal_worker:
            run_options["lexical_deadline_seconds"] = args.deadline_seconds
        if report is not None:
            pass
        elif args.cache_root is None:
            with tempfile.TemporaryDirectory(prefix="llm-wiki-retrieval-v2-") as temporary:
                report = run_benchmark(
                    corpus,
                    cache_root=Path(temporary),
                    **run_options,
                )
        else:
            report = run_benchmark(
                corpus,
                cache_root=args.cache_root,
                **run_options,
            )
        if args.adapter == MODEL_MATRIX_ADAPTER_KIND:
            serialized = _canonical_report_bytes(report).decode("utf-8")
        else:
            serialized = json.dumps(
                report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            ) + "\n"
        if args.output is not None:
            requested_output = args.output.expanduser()
            if requested_output.is_symlink():
                raise ValueError("--output must not be a symlink")
            if not requested_output.is_absolute():
                requested_output = Path.cwd() / requested_output
            output = requested_output.parent.resolve() / requested_output.name
            if output.exists() or output.is_symlink():
                raise ValueError("--output must not already exist")
            forbidden = [ROOT.resolve()]
            configured_vault = os.environ.get("LLM_WIKI_ROOT")
            if configured_vault:
                forbidden.append(Path(configured_vault).expanduser().resolve())
            if any(output == root or root in output.parents for root in forbidden):
                raise ValueError("--output must be outside source and vault roots")
            output.parent.mkdir(parents=True, exist_ok=True)
            output = output.parent.resolve(strict=True) / output.name
            _write_new_output(output, serialized)
        if args.json:
            print(serialized, end="")
        else:
            print(
                f"retrieval-v2 {report.get('adapter_kind', args.adapter)} complete; "
                f"quality_claim={str(report['quality_claim']).lower()}; "
                f"release_evidence={str(report.get('release_evidence', False)).lower()}"
            )
        return (
            0
            if report["gates"]["passed_for_orchestration"]
            and not report["gates"].get("degraded", False)
            else 2
        )
    except (OSError, ValueError, SchemaValidationError) as exc:
        print(f"retrieval-v2 error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
