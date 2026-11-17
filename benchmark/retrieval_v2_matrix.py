"""Validate the model-policy matrix and derive the model selection and the candidate
specifications it requires."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from retrieval_v2_contract import MAX_CORPUS_BYTES, SELECTION_ARTIFACT_FIELDS, ModelSelection
from retrieval_v2_environment import sha256_file

# isort: split
from bounded_io import read_stable_bytes
from reliable_memory import canonical_json_bytes


def require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} is not a closed canonical matrix object")


_CANDIDATE_FIELDS = frozenset(
    {
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
)
_EMBEDDING_FORMATTING_KEYS = frozenset({"document", "instruction", "query"})
_EMBEDDING_INFERENCE_KEYS = frozenset(
    {"l2_normalize", "max_length_tokens", "padding_side", "pooling", "truncation_side"}
)
_RERANKER_FORMATTING_KEYS = frozenset(
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
    }
)
_VARIANT_FIELDS = frozenset({"dimensions", "precision", "quality", "resource_measurements", "variant_id"})
_QUALITY_KEYS = frozenset({"claim", "overall", "per_language", "status"})
_RESOURCE_KEYS = frozenset(
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
    }
)
_MRL_KEYS = frozenset({"enabled", "renormalize_after_truncation", "truncate_to_dimensions"})


def _candidate_fields(kind: str) -> set[str]:
    fields = set(_CANDIDATE_FIELDS)
    if kind == "embedding":
        fields.add("inference")
    return fields


def _require_candidate_identity(candidate: dict, kind: str) -> None:
    if candidate["kind"] != kind:
        raise ValueError(f"matrix {kind} kind mismatch")
    _require_pinned_local_code(candidate, kind)


def _require_pinned_local_code(candidate: dict, kind: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate["revision"]):
        raise ValueError(f"matrix {kind} revision is not pinned")
    if candidate["trust_remote_code"] is not False:
        raise ValueError(f"matrix {kind} requires remote code")


def _require_candidate_contract(candidate: dict, kind: str, contract: dict) -> None:
    if candidate["cache_policy"] != contract["cache_policy"]:
        raise ValueError(f"matrix {kind} cache policy mismatch")
    if candidate["batch_size"] != contract["batch_size"]:
        raise ValueError(f"matrix {kind} batch size mismatch")


def _require_shippable(candidate: dict, kind: str) -> None:
    if candidate["license"] not in {"Apache-2.0", "MIT"} or candidate["exclusion_reasons"]:
        raise ValueError(f"matrix {kind} shipping policy is inconsistent")


def _require_shipping_policy(candidate: dict, kind: str) -> None:
    if candidate["shipping_eligible"]:
        _require_shippable(candidate, kind)
        return
    if not candidate["exclusion_reasons"]:
        raise ValueError(f"matrix {kind} exclusion lacks a reason")


def _require_candidate_license(candidate: dict, kind: str, contract: dict) -> None:
    if candidate["license"] not in {"Apache-2.0", "Gemma", "MIT"}:
        raise ValueError(f"matrix {kind} license is unknown")
    _require_shipping_policy(candidate, kind)
    if not candidate["languages"] or not set(candidate["languages"]) <= set(contract["languages"]):
        raise ValueError(f"matrix {kind} language coverage is invalid")


def _require_native_library(candidate: dict, kind: str) -> None:
    library = candidate["native_library"]
    require_exact_keys(library, {"name", "support"}, "native library")
    if library["name"] not in {"sentence-transformers", "transformers"} or library["support"] != "native":
        raise ValueError(f"matrix {kind} lacks native library support")


def _require_candidate_source(candidate: dict, kind: str) -> None:
    if candidate["source_url"] != f"https://huggingface.co/{candidate['id']}/tree/{candidate['revision']}":
        raise ValueError(f"matrix {kind} source is not revision pinned")
    if not candidate["variants"]:
        raise ValueError(f"matrix {kind} has no explicit variants")


def _require_candidate_formatting(candidate: dict, kind: str) -> None:
    if kind != "embedding":
        require_exact_keys(candidate["formatting"], set(_RERANKER_FORMATTING_KEYS), "reranker formatting")
        return
    require_exact_keys(candidate["formatting"], set(_EMBEDDING_FORMATTING_KEYS), "embedding formatting")
    require_exact_keys(candidate["inference"], set(_EMBEDDING_INFERENCE_KEYS), "embedding inference")
    if not candidate["formatting"]["query"] or not candidate["formatting"]["document"]:
        raise ValueError("embedding candidate lacks explicit formatting defaults")


def _variant_fields(kind: str) -> set[str]:
    if kind == "embedding":
        return {*_VARIANT_FIELDS, "mrl"}
    return set(_VARIANT_FIELDS)


def _require_variant(variant: dict, kind: str, contract: dict) -> None:
    require_exact_keys(variant, _variant_fields(kind), f"{kind} variant")
    require_exact_keys(variant["quality"], set(_QUALITY_KEYS), "variant quality")
    require_exact_keys(variant["quality"]["per_language"], {"EN", "RU", "ZH"}, "variant languages")
    require_exact_keys(variant["resource_measurements"], set(_RESOURCE_KEYS), "variant resource measurements")
    if variant["precision"] != contract["precision"]:
        raise ValueError(f"matrix {kind} variant precision mismatch")
    if kind == "embedding":
        require_exact_keys(variant["mrl"], set(_MRL_KEYS), "embedding MRL")


def _validate_matrix_candidate(candidate: dict, *, kind: str, contract: dict) -> None:
    require_exact_keys(candidate, _candidate_fields(kind), f"{kind} candidate")
    _require_candidate_identity(candidate, kind)
    _require_candidate_contract(candidate, kind, contract)
    _require_candidate_license(candidate, kind, contract)
    _require_native_library(candidate, kind)
    _require_candidate_source(candidate, kind)
    _require_candidate_formatting(candidate, kind)
    for variant in candidate["variants"]:
        _require_variant(variant, kind, contract)


_MATRIX_KEYS = frozenset(
    {"artifact_kind", "benchmark_contract", "embeddings", "lexical", "rerankers", "schema_version", "selection"}
)
_CONTRACT_KEYS = frozenset(
    {"batch_size", "cache_policy", "corpus", "languages", "precision", "quality_claims_allowed", "reranker_depths"}
)
_SELECTION_KEYS = frozenset(
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
    }
)
_AGGREGATION_CONTRACT_KEYS = frozenset(
    {"artifact_schema", "complete_candidate_set", "output_path_policy", "required_fields", "schema_version"}
)
_BASELINE_KEYS = frozenset(
    {
        "overall_basis_points",
        "parent_recall_at_10_basis_points",
        "policy_sha256",
        "raw_report_path",
        "raw_report_sha256",
    }
)
_LEXICAL_POLICY_KEYS = frozenset(
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
    }
)
_BENCHMARK_CACHE_POLICY = {"network_access": "prefetch_only", "offline_required": True, "revision_scoped": True}


def _parsed_matrix(raw: bytes) -> dict:
    try:
        matrix = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot load model matrix: {exc}") from exc
    if canonical_json_bytes(matrix) + b"\n" != raw:
        raise ValueError("model matrix bytes are not canonical and frozen")
    return matrix


def _require_matrix_shape(matrix: dict) -> None:
    require_exact_keys(matrix, set(_MATRIX_KEYS), "model matrix")
    if matrix["artifact_kind"] != "model-policy-matrix" or matrix["schema_version"] != 1:
        raise ValueError("unsupported model matrix")


def _require_contract_shape(contract: dict) -> None:
    require_exact_keys(contract, set(_CONTRACT_KEYS), "benchmark contract")
    require_exact_keys(
        contract["cache_policy"], {"network_access", "offline_required", "revision_scoped"}, "benchmark cache policy"
    )
    require_exact_keys(contract["corpus"], {"path", "sha256"}, "benchmark corpus")


def _require_aggregation_contract(aggregation_contract: dict) -> None:
    require_exact_keys(aggregation_contract, set(_AGGREGATION_CONTRACT_KEYS), "aggregation evidence contract")
    if set(aggregation_contract["required_fields"]) != SELECTION_ARTIFACT_FIELDS:
        raise ValueError("aggregation evidence fields do not match runner contract")
    if (
        aggregation_contract["schema_version"] != 1
        or aggregation_contract["artifact_schema"] != "retrieval-selection/v1"
        or aggregation_contract["output_path_policy"] != "normalized_repo_relative_json_under_benchmark_results"
    ):
        raise ValueError("unsupported aggregation evidence contract")


def _require_selection_shape(selection: dict) -> None:
    require_exact_keys(selection, set(_SELECTION_KEYS), "matrix selection")
    _require_aggregation_contract(selection["aggregation_evidence_contract"])
    require_exact_keys(selection["baseline"], set(_BASELINE_KEYS), "selection baseline")
    require_exact_keys(selection["limits"], {"peak_rss_bytes", "warm_p95_ms"}, "selection limits")
    require_exact_keys(
        selection["material_improvement"],
        {"metric", "minimum_absolute_gain_basis_points"},
        "selection material improvement",
    )


def _require_contract_values(contract: dict) -> None:
    if contract["precision"] != "float32" or contract["batch_size"] != 8:
        raise ValueError("unsupported benchmark precision or batch size")
    if contract["quality_claims_allowed"] is not True:
        raise ValueError("matrix does not permit provenance-bound candidate quality claims")


def _require_contract_policy(contract: dict) -> None:
    if contract["cache_policy"] != _BENCHMARK_CACHE_POLICY:
        raise ValueError("unsupported benchmark cache policy")
    if contract["reranker_depths"] != [10, 20, 50]:
        raise ValueError("unsupported reranker depths")


def _verified_corpus_hash(corpus_path: Path, contract: dict) -> str:
    corpus_hash = sha256_file(corpus_path)
    if corpus_hash != contract["corpus"]["sha256"]:
        raise ValueError("benchmark corpus SHA256 does not match matrix")
    return corpus_hash


def _require_matrix_candidates(matrix: dict, contract: dict) -> None:
    for candidate in matrix["embeddings"]:
        _validate_matrix_candidate(candidate, kind="embedding", contract=contract)
    for candidate in matrix["rerankers"]:
        _validate_matrix_candidate(candidate, kind="reranker", contract=contract)


def _selected_embedding(matrix: dict, model_id: str) -> dict:
    embedding = next((item for item in matrix["embeddings"] if item["id"] == model_id), None)
    if embedding is None:
        raise ValueError(f"unknown embedding model: {model_id}")
    return embedding


def _require_pinned_variant(variant: dict) -> None:
    if variant["precision"] != "float32" or not isinstance(variant["dimensions"], int):
        raise ValueError("embedding variant is not pinned to float32 dimensions")


def _selected_variant(embedding: dict, variant_id: str) -> dict:
    variant = next((item for item in embedding["variants"] if item["variant_id"] == variant_id), None)
    if variant is None:
        raise ValueError(f"unknown embedding variant: {variant_id}")
    _require_pinned_variant(variant)
    return variant


def _reranker_variant(reranker: dict) -> dict:
    if len(reranker["variants"]) != 1:
        raise ValueError("reranker lacks one explicit matrix variant")
    reranker_variant = reranker["variants"][0]
    if reranker_variant["precision"] != "float32":
        raise ValueError("reranker variant is not pinned to float32")
    return reranker_variant


def _selected_reranker(matrix: dict, reranker_id: str | None) -> tuple[dict | None, dict | None]:
    if reranker_id is None:
        return None, None
    reranker = next((item for item in matrix["rerankers"] if item["id"] == reranker_id), None)
    if reranker is None:
        raise ValueError(f"unknown reranker model: {reranker_id}")
    return reranker, _reranker_variant(reranker)


def load_model_selection(
    matrix_path: Path | str,
    corpus_path: Path | str,
    *,
    model_id: str,
    variant_id: str,
    reranker_id: str | None = None,
) -> ModelSelection:
    raw = read_stable_bytes(Path(matrix_path), MAX_CORPUS_BYTES, label="model matrix")
    matrix = _parsed_matrix(raw)
    _require_matrix_shape(matrix)
    contract = matrix["benchmark_contract"]
    _require_contract_shape(contract)
    _require_selection_shape(matrix["selection"])
    require_exact_keys(matrix["lexical"], set(_LEXICAL_POLICY_KEYS), "matrix lexical policy")
    _require_contract_values(contract)
    _require_contract_policy(contract)
    corpus_hash = _verified_corpus_hash(Path(corpus_path), contract)
    _require_matrix_candidates(matrix, contract)
    embedding = _selected_embedding(matrix, model_id)
    variant = _selected_variant(embedding, variant_id)
    reranker, reranker_variant = _selected_reranker(matrix, reranker_id)
    return ModelSelection(
        matrix=matrix,
        embedding=embedding,
        variant=variant,
        reranker=reranker,
        reranker_variant=reranker_variant,
        matrix_sha256=hashlib.sha256(raw).hexdigest(),
        corpus_sha256=corpus_hash,
    )


def matrix_target(candidate: dict, variant: dict) -> dict:
    return {
        "dimensions": variant["dimensions"],
        "id": candidate["id"],
        "revision": candidate["revision"],
        "variant_id": variant["variant_id"],
    }


def required_candidate_specs(matrix: dict) -> list[dict]:
    rerankers = [
        matrix_target(candidate, candidate["variants"][0])
        for candidate in matrix["rerankers"]
    ]
    return [
        {"embedding": matrix_target(candidate, variant), "reranker": reranker}
        for candidate in matrix["embeddings"]
        for variant in candidate["variants"]
        for reranker in [None, *rerankers]
    ]
