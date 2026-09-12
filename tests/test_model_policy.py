from __future__ import annotations

import ast
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "benchmark" / "model-matrix-v1.json"
RUNNER = ROOT / "benchmark" / "run_retrieval_v2.py"
MODEL_KINDS = {"embedding", "reranker"}
KNOWN_LICENSES = {"Apache-2.0", "Gemma", "MIT"}
TARGET_LANGUAGES = {"EN", "RU", "ZH"}
RESOURCE_MEASUREMENT_FIELDS = {
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
QUALITY_FIELDS = {"claim", "overall", "per_language", "status"}
SELECTION_TARGET_FIELDS = {"dimensions", "id", "revision", "variant_id"}
EVIDENCE_FIELDS = {
    "benchmark_contract_sha256",
    "benchmark_corpus_sha256",
    "gates",
    "matrix_policy_sha256",
    "measured_at",
    "measurements",
    "pareto",
    "raw_report_path",
    "raw_report_sha256",
    "selected",
}
RAW_REPORT_FIELDS = EVIDENCE_FIELDS - {"raw_report_path", "raw_report_sha256"}
GATE_FIELDS = {
    "no_parent_recall_at_10_regression",
    "overall",
    "per_language",
    "required",
    "shipping_eligible",
}


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _load() -> dict:
    return json.loads(MATRIX.read_bytes())


def _assigns_name(node: ast.stmt, name: str) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    return any(
        isinstance(target, ast.Name) and target.id == name for target in node.targets
    )


def _runner_literal(name: str) -> object:
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"), filename=str(RUNNER))
    for node in tree.body:
        if _assigns_name(node, name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"runner constant not found: {name}")


def _assert_keys(value: dict, expected: set[str]) -> None:
    assert set(value) == expected


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value)[:-1])


def _reset_quality(quality: dict) -> None:
    quality["claim"] = None
    quality["overall"] = None
    quality["status"] = "unmeasured"
    quality["per_language"] = {language: None for language in sorted(TARGET_LANGUAGES)}


def _reset_resources(resources: dict) -> None:
    for field in RESOURCE_MEASUREMENT_FIELDS - {"status"}:
        resources[field] = None
    resources["status"] = "unmeasured"


def _matrix_policy_fingerprint(matrix: dict) -> str:
    policy = deepcopy(matrix)
    selection = policy["selection"]
    selection["default_embedding"] = None
    selection["default_reranker"] = None
    selection["result_evidence"] = None
    selection["status"] = "awaiting_raw_benchmark"
    for candidate in policy["embeddings"] + policy["rerankers"]:
        for variant in candidate["variants"]:
            _reset_quality(variant["quality"])
            _reset_resources(variant["resource_measurements"])
    _reset_quality(policy["lexical"]["quality"])
    _reset_resources(policy["lexical"]["resource_measurements"])
    return _sha256_json(policy)


def _resolve_target(matrix: dict, target: dict) -> tuple[dict, dict]:
    _assert_keys(target, SELECTION_TARGET_FIELDS)
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", target["variant_id"])
    candidates = matrix["embeddings"] + matrix["rerankers"]
    candidate = next(item for item in candidates if item["id"] == target["id"])
    assert target["revision"] == candidate["revision"]
    variant = next(
        item for item in candidate["variants"] if item["variant_id"] == target["variant_id"]
    )
    assert target["dimensions"] == variant["dimensions"]
    return candidate, variant


def _validate_quality(quality: dict) -> None:
    _assert_keys(quality, QUALITY_FIELDS)
    _assert_keys(quality["per_language"], TARGET_LANGUAGES)
    assert quality["claim"] is None
    values = [quality["overall"], *quality["per_language"].values()]
    if quality["status"] == "unmeasured":
        assert all(value is None for value in values)
    else:
        assert quality["status"] == "measured"
        assert all(type(value) in {int, float} and 0 <= value <= 1 for value in values)


def _is_nonnegative_number(value: object) -> bool:
    return type(value) in {int, float} and value >= 0


def _resource_values(resources: dict) -> list:
    return [value for key, value in resources.items() if key != "status"]


def _unmeasured_resources(resources: dict) -> bool:
    return resources["status"] == "unmeasured"


def _validate_resources(resources: dict) -> None:
    _assert_keys(resources, RESOURCE_MEASUREMENT_FIELDS)
    values = _resource_values(resources)
    if _unmeasured_resources(resources):
        assert set(values) == {None}
        return
    unusable = [value for value in values if not _is_nonnegative_number(value)]
    assert (resources["status"], unusable) == ("measured", [])


def _validate_objective_values(matrix: dict, variant: dict, values: dict) -> None:
    objectives = matrix["selection"]["pareto_objectives"]
    _assert_keys(values, set(objectives))
    expected = {
        "index_bytes": variant["resource_measurements"]["index_bytes"],
        "overall": variant["quality"]["overall"],
        "peak_rss_bytes": variant["resource_measurements"]["peak_rss_bytes"],
        "warm_p95_ms": variant["resource_measurements"]["warm_p95_ms"],
    }
    assert values == expected


def _gate_outcomes_pass(gates: dict, required_gates: list[str]) -> bool:
    _assert_keys(gates, GATE_FIELDS)
    _assert_keys(gates["per_language"], TARGET_LANGUAGES)
    _assert_keys(gates["required"], set(required_gates))
    values = [
        gates["overall"],
        gates["no_parent_recall_at_10_regression"],
        gates["shipping_eligible"],
        *gates["per_language"].values(),
        *gates["required"].values(),
    ]
    assert all(type(value) is bool for value in values)
    return all(values)


def _dominates(candidate: dict, selected: dict, objectives: dict) -> bool:
    weakly_better = []
    strictly_better = []
    for field, direction in objectives.items():
        assert direction in {"maximize", "minimize"}
        candidate_value = candidate[field]
        selected_value = selected[field]
        if direction == "minimize":
            weakly_better.append(candidate_value <= selected_value)
            strictly_better.append(candidate_value < selected_value)
        else:
            weakly_better.append(candidate_value >= selected_value)
            strictly_better.append(candidate_value > selected_value)
    return all(weakly_better) and any(strictly_better)


_MEASURED_AT_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def _target_key(target: dict) -> tuple:
    return tuple(target[field] for field in sorted(SELECTION_TARGET_FIELDS))


def _variant_target(item: dict, variant: dict) -> dict:
    return {
        "dimensions": variant["dimensions"],
        "id": item["id"],
        "revision": item["revision"],
        "variant_id": variant["variant_id"],
    }


def _shipping_target_keys(candidates: list) -> set:
    return {
        _target_key(_variant_target(item, variant))
        for item in candidates
        if item["shipping_eligible"]
        for variant in item["variants"]
    }


def _validate_awaiting_selection(selection: dict) -> None:
    """No default yet: the matrix must say so and carry no evidence."""
    assert selection["status"] == "awaiting_raw_benchmark"
    assert selection["result_evidence"] is None


def _validate_measured_variant(variant: dict) -> None:
    _validate_quality(variant["quality"])
    _validate_resources(variant["resource_measurements"])
    assert variant["quality"]["status"] == "measured"
    assert variant["resource_measurements"]["status"] == "measured"


def _validate_evidence_fingerprints(matrix: dict, evidence: dict) -> None:
    corpus = matrix["benchmark_contract"]["corpus"]
    assert evidence["matrix_policy_sha256"] == _matrix_policy_fingerprint(matrix)
    assert evidence["benchmark_contract_sha256"] == _sha256_json(
        matrix["benchmark_contract"]
    )
    assert evidence["benchmark_corpus_sha256"] == corpus["sha256"]
    assert _sha256_bytes((ROOT / corpus["path"]).read_bytes()) == corpus["sha256"]


def _unusable_report_path_parts(path: PurePosixPath) -> list:
    """Path shapes a published report path may not have."""
    refusals = (
        ("absolute", path.is_absolute()),
        ("dot", "." in path.parts),
        ("dotdot", ".." in path.parts),
        ("root", path.parts[:2] != ("benchmark", "results")),
        ("suffix", path.suffix != ".json"),
    )
    return [name for name, wrong in refusals if wrong]


def _validate_raw_report(evidence: dict, raw_reports: dict[str, bytes] | None) -> None:
    report_path = evidence["raw_report_path"]
    path = PurePosixPath(report_path)
    assert (path.as_posix(), _unusable_report_path_parts(path)) == (report_path, [])
    assert raw_reports is not None and report_path in raw_reports
    assert evidence["raw_report_sha256"] == _sha256_bytes(raw_reports[report_path])
    report = json.loads(raw_reports[report_path])
    _assert_keys(report, RAW_REPORT_FIELDS)
    assert report == {field: evidence[field] for field in RAW_REPORT_FIELDS}


def _validate_measured_at(evidence: dict) -> None:
    measured_at = evidence["measured_at"]
    assert re.fullmatch(_MEASURED_AT_PATTERN, measured_at)
    assert datetime.fromisoformat(measured_at.replace("Z", "+00:00")).tzinfo is not None


def _validate_pareto_candidate(matrix: dict, item: dict) -> None:
    _assert_keys(item, {"gates", "objective_values", "target"})
    _, item_variant = _resolve_target(matrix, item["target"])
    _validate_measured_variant(item_variant)
    _validate_objective_values(matrix, item_variant, item["objective_values"])


def _dominating_candidates(selected: dict, candidates: list, objectives: list) -> list:
    return [
        item
        for item in candidates
        if item is not selected
        if _dominates(item["objective_values"], selected["objective_values"], objectives)
    ]


def _gate_passing(candidates: list, required: list) -> list:
    return [item for item in candidates if _gate_outcomes_pass(item["gates"], required)]


def _selected_pareto_candidate(candidates: list, target: dict) -> dict:
    return next(item for item in candidates if item["target"] == target)


def _validate_pareto(matrix: dict, selection: dict, evidence: dict, target: dict) -> None:
    pareto = evidence["pareto"]
    _assert_keys(pareto, {"candidates"})
    assert pareto["candidates"]
    for item in pareto["candidates"]:
        _validate_pareto_candidate(matrix, item)
    required = selection["required_gates"]
    selected = _selected_pareto_candidate(pareto["candidates"], target)
    passing = _gate_passing(pareto["candidates"], required)
    assert _gate_outcomes_pass(selected["gates"], required)
    assert _dominating_candidates(selected, passing, selection["pareto_objectives"]) == []


def _validate_evidenced_targets(matrix: dict, evidence: dict, candidate: dict) -> None:
    kind_candidates = (
        matrix["embeddings"] if candidate["kind"] == "embedding" else matrix["rerankers"]
    )
    evidenced = {_target_key(item["target"]) for item in evidence["pareto"]["candidates"]}
    assert evidenced == _shipping_target_keys(kind_candidates)


def _validate_selection(matrix: dict, raw_reports: dict[str, bytes] | None = None) -> None:
    selection = matrix["selection"]
    selected = [
        item
        for item in (selection["default_embedding"], selection["default_reranker"])
        if item is not None
    ]
    if not selected:
        _validate_awaiting_selection(selection)
        return
    assert (len(selected), selection["status"]) == (1, "selected_from_raw_benchmark")
    evidence = selection["result_evidence"]
    _assert_keys(evidence, EVIDENCE_FIELDS)
    target = selected[0]
    candidate, variant = _resolve_target(matrix, target)
    _validate_measured_variant(variant)
    _assert_keys(evidence["measurements"], {"quality", "resources"})
    assert (evidence["selected"], candidate["shipping_eligible"]) == (target, True)
    assert evidence["measurements"] == {
        "quality": variant["quality"],
        "resources": variant["resource_measurements"],
    }
    assert _gate_outcomes_pass(evidence["gates"], selection["required_gates"])
    _validate_evidence_fingerprints(matrix, evidence)
    _validate_raw_report(evidence, raw_reports)
    _validate_measured_at(evidence)
    _validate_pareto(matrix, selection, evidence, target)
    _validate_evidenced_targets(matrix, evidence, candidate)


def _embedding_named(matrix: dict, model_id: str) -> dict:
    return next(item for item in matrix["embeddings"] if item["id"] == model_id)


def _variant_with_dimensions(candidate: dict, dimensions: int) -> dict:
    return next(
        item for item in candidate["variants"] if item["dimensions"] == dimensions
    )


def _objective_values(variant: dict) -> dict:
    resources = variant["resource_measurements"]
    return {
        "index_bytes": resources["index_bytes"],
        "overall": variant["quality"]["overall"],
        "peak_rss_bytes": resources["peak_rss_bytes"],
        "warm_p95_ms": resources["warm_p95_ms"],
    }


def _pareto_candidates(embeddings: list, gates: dict) -> list:
    """One candidate per shipping-eligible variant, all with the same gates."""
    return [
        {
            "gates": deepcopy(gates),
            "objective_values": _objective_values(variant),
            "target": _variant_target(item, variant),
        }
        for item in embeddings
        if item["shipping_eligible"]
        for variant in item["variants"]
    ]


def _synthetic_quality(offset: int) -> dict:
    return {
        "claim": None,
        "overall": 0.7 + offset / 1000,
        "per_language": {
            "EN": 0.71 + offset / 1000,
            "RU": 0.69 + offset / 1000,
            "ZH": 0.7 + offset / 1000,
        },
        "status": "measured",
    }


def _synthetic_resources(offset: int, dimensions: int) -> dict:
    return {
        "cold_first_query_ms": 40.0 + offset,
        "index_bytes": 300000 + offset * 1000,
        "indexing_throughput_documents_per_second": 100.0 - offset,
        "model_load_ms": 200.0 + offset,
        "peak_rss_bytes": 1000000000 + offset * 1000000,
        "status": "measured",
        "vector_bytes_per_document": dimensions * 4,
        "warm_p50_ms": 7.0 + offset,
        "warm_p95_ms": 10.0 + offset,
    }


def _fill_measured_embeddings(embeddings: list) -> None:
    """Give every variant a distinct measured row, so ordering is unambiguous."""
    for model_index, item in enumerate(embeddings):
        for variant_index, variant in enumerate(item["variants"]):
            offset = model_index * 10 + variant_index
            variant["quality"] = _synthetic_quality(offset)
            variant["resource_measurements"] = _synthetic_resources(
                offset, variant["dimensions"]
            )


def _synthetic_measured_matrix() -> tuple[dict, dict[str, bytes]]:
    matrix = deepcopy(_load())
    _fill_measured_embeddings(matrix["embeddings"])
    candidate = _embedding_named(matrix, "Qwen/Qwen3-Embedding-0.6B")
    variant = _variant_with_dimensions(candidate, 384)
    target = {
        "dimensions": 384,
        "id": candidate["id"],
        "revision": candidate["revision"],
        "variant_id": variant["variant_id"],
    }
    report_path = "benchmark/results/synthetic-measured.json"
    passing_gates = {
        "no_parent_recall_at_10_regression": True,
        "overall": True,
        "per_language": {"EN": True, "RU": True, "ZH": True},
        "required": {gate: True for gate in matrix["selection"]["required_gates"]},
        "shipping_eligible": True,
    }
    evidence = {
        "benchmark_contract_sha256": _sha256_json(matrix["benchmark_contract"]),
        "benchmark_corpus_sha256": matrix["benchmark_contract"]["corpus"]["sha256"],
        "gates": deepcopy(passing_gates),
        "matrix_policy_sha256": "",
        "measured_at": "2026-07-17T12:00:00Z",
        "measurements": {
            "quality": deepcopy(variant["quality"]),
            "resources": deepcopy(variant["resource_measurements"]),
        },
        "pareto": {"candidates": _pareto_candidates(matrix["embeddings"], passing_gates)},
        "raw_report_path": report_path,
        "raw_report_sha256": "",
        "selected": deepcopy(target),
    }
    matrix["selection"]["default_embedding"] = target
    matrix["selection"]["result_evidence"] = evidence
    matrix["selection"]["status"] = "selected_from_raw_benchmark"
    evidence["matrix_policy_sha256"] = _matrix_policy_fingerprint(matrix)
    report = _canonical_bytes({field: evidence[field] for field in RAW_REPORT_FIELDS})
    evidence["raw_report_sha256"] = _sha256_bytes(report)
    return matrix, {report_path: report}


def test_matrix_exists_and_has_canonical_compact_sorted_bytes():
    assert MATRIX.is_file()
    raw = MATRIX.read_bytes()
    matrix = json.loads(raw)

    assert raw == _canonical_bytes(matrix)
    assert b"\n " not in raw


_CANDIDATE_FIELDS = {
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
_VARIANT_FIELDS = {
    "dimensions",
    "precision",
    "quality",
    "resource_measurements",
    "variant_id",
}
_MRL_FIELDS = {"enabled", "renormalize_after_truncation", "truncate_to_dimensions"}
_CACHE_POLICY_FIELDS = {"network_access", "offline_required", "revision_scoped"}
_INFERENCE_FIELDS = {
    "l2_normalize",
    "max_length_tokens",
    "padding_side",
    "pooling",
    "truncation_side",
}
_EMBEDDING_FORMATTING_FIELDS = {"document", "instruction", "query"}
_RERANKER_FORMATTING_FIELDS = {
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


def _assert_embedding_formatting(candidate: dict) -> None:
    _assert_keys(candidate["formatting"], _EMBEDDING_FORMATTING_FIELDS)
    _assert_keys(candidate["inference"], _INFERENCE_FIELDS)


def _assert_reranker_formatting(candidate: dict) -> None:
    _assert_keys(candidate["formatting"], _RERANKER_FORMATTING_FIELDS)
    if candidate["formatting"]["score_tokens"] is None:
        return
    _assert_keys(candidate["formatting"]["score_tokens"], {"negative", "positive"})


def _assert_candidate_shape(candidate: dict) -> None:
    """A candidate's own fields, plus the ones only one kind carries."""
    expected = set(_CANDIDATE_FIELDS)
    expected.update({"inference"} if candidate["kind"] == "embedding" else set())
    _assert_keys(candidate, expected)
    _assert_keys(candidate["cache_policy"], _CACHE_POLICY_FIELDS)
    _assert_keys(candidate["native_library"], {"name", "support"})
    _assert_formatting_shape(candidate)


def _assert_formatting_shape(candidate: dict) -> None:
    if candidate["kind"] == "embedding":
        _assert_embedding_formatting(candidate)
        return
    _assert_reranker_formatting(candidate)


def _assert_mrl_shape(candidate: dict, variant: dict) -> None:
    if candidate["kind"] != "embedding":
        return
    _assert_keys(variant["mrl"], _MRL_FIELDS)


def _assert_variant_shape(candidate: dict, variant: dict) -> None:
    expected = set(_VARIANT_FIELDS)
    expected.update({"mrl"} if candidate["kind"] == "embedding" else set())
    _assert_keys(variant, expected)
    _assert_mrl_shape(candidate, variant)
    _assert_keys(variant["quality"], QUALITY_FIELDS)
    _assert_keys(variant["quality"]["per_language"], TARGET_LANGUAGES)
    _assert_keys(variant["resource_measurements"], RESOURCE_MEASUREMENT_FIELDS)


def test_matrix_is_closed_at_every_policy_object_level():
    matrix = _load()
    _assert_keys(
        matrix,
        {
            "artifact_kind",
            "benchmark_contract",
            "embeddings",
            "lexical",
            "rerankers",
            "schema_version",
            "selection",
        },
    )
    _assert_keys(
        matrix["benchmark_contract"],
        {
            "batch_size",
            "cache_policy",
            "corpus",
            "languages",
            "precision",
            "quality_claims_allowed",
            "reranker_depths",
        },
    )
    _assert_keys(
        matrix["benchmark_contract"]["cache_policy"],
        {"network_access", "offline_required", "revision_scoped"},
    )
    _assert_keys(matrix["benchmark_contract"]["corpus"], {"path", "sha256"})
    _assert_keys(
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
    )
    assert matrix["selection"]["aggregation_evidence_contract"] == {
        "artifact_schema": "retrieval-selection/v1",
        "complete_candidate_set": (
            "all required embedding variants crossed with no reranker and every required reranker"
        ),
        "output_path_policy": "normalized_repo_relative_json_under_benchmark_results",
        "required_fields": [
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
        ],
        "schema_version": 1,
    }
    _assert_keys(
        matrix["selection"]["baseline"],
        {
            "overall_basis_points",
            "parent_recall_at_10_basis_points",
            "policy_sha256",
            "raw_report_path",
            "raw_report_sha256",
        },
    )
    _assert_keys(matrix["selection"]["limits"], {"peak_rss_bytes", "warm_p95_ms"})
    _assert_keys(
        matrix["selection"]["material_improvement"],
        {"metric", "minimum_absolute_gain_basis_points"},
    )
    _assert_keys(
        matrix["selection"]["result_evidence_contract"],
        {
            "fingerprint_algorithm",
            "gate_fields",
            "matrix_policy_scope",
            "raw_report_path_policy",
            "required_fields",
            "schema_version",
        },
    )
    for candidate in matrix["embeddings"] + matrix["rerankers"]:
        _assert_candidate_shape(candidate)
        assert candidate["variants"]
        for variant in candidate["variants"]:
            _assert_variant_shape(candidate, variant)
    lexical = matrix["lexical"]
    _assert_keys(
        lexical,
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
    )
    _assert_keys(lexical["quality"], QUALITY_FIELDS)
    _assert_keys(lexical["quality"]["per_language"], TARGET_LANGUAGES)
    _assert_keys(lexical["resource_measurements"], RESOURCE_MEASUREMENT_FIELDS)


_VARIANT_ID_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_REVISION_PATTERN = r"[0-9a-f]{40}"
_NATIVE_LIBRARIES = {"sentence-transformers", "transformers"}


def _normalized_id(model: dict) -> str:
    return model["id"].strip().casefold()


def _model_ids_sorted(candidates: list) -> bool:
    ids = [_normalized_id(candidate) for candidate in candidates]
    return ids == sorted(ids)


def _model_contract_violations(model: dict) -> list:
    """Every field of a pinned model that does not match the policy contract."""
    checks = (
        ("kind", model["kind"] in MODEL_KINDS),
        ("revision", bool(re.fullmatch(_REVISION_PATTERN, model["revision"]))),
        (
            "source_url",
            model["source_url"]
            == f"https://huggingface.co/{model['id']}/tree/{model['revision']}",
        ),
        ("license", model["license"] in KNOWN_LICENSES),
        ("trust_remote_code", model["trust_remote_code"] is False),
        ("native_library", model["native_library"]["name"] in _NATIVE_LIBRARIES),
        ("native_support", model["native_library"]["support"] == "native"),
        ("shipping_eligible", type(model["shipping_eligible"]) is bool),
    )
    return [name for name, holds in checks if not holds]


def _usable_dimension(dimension: object) -> bool:
    return dimension is None or (type(dimension) is int and dimension > 0)


def _named_variant_ids(variant_ids: list) -> bool:
    return all(re.fullmatch(_VARIANT_ID_PATTERN, item) for item in variant_ids)


def _usable_dimensions(dimensions: list) -> bool:
    return all(_usable_dimension(item) for item in dimensions)


def _variant_contract_violations(model: dict) -> list:
    """Variant identities and dimensions: named, unique, and usable."""
    variant_ids = [variant["variant_id"] for variant in model["variants"]]
    dimensions = [variant["dimensions"] for variant in model["variants"]]
    checks = (
        ("variant_id_pattern", _named_variant_ids(variant_ids)),
        ("variant_id_unique", len(variant_ids) == len(set(variant_ids))),
        ("dimensions_unique", len(dimensions) == len(set(dimensions))),
        ("dimensions_usable", _usable_dimensions(dimensions)),
    )
    return [name for name, holds in checks if not holds]


def test_models_have_unique_normalized_ids_and_immutable_sources():
    matrix = _load()
    models = matrix["embeddings"] + matrix["rerankers"]
    normalized = [_normalized_id(model) for model in models]
    orders = [_model_ids_sorted(matrix["embeddings"]), _model_ids_sorted(matrix["rerankers"])]

    assert (len(normalized), orders) == (len(set(normalized)), [True, True])
    for model in models:
        assert _model_contract_violations(model) == []
        assert _variant_contract_violations(model) == []


def test_required_embedding_pins_and_formatting_are_exact():
    embeddings = {model["id"]: model for model in _load()["embeddings"]}
    assert set(embeddings) == {
        "BAAI/bge-m3",
        "BAAI/bge-small-en-v1.5",
        "Qwen/Qwen3-Embedding-0.6B",
        "google/embeddinggemma-300m",
        "intfloat/multilingual-e5-large-instruct",
    }

    e5 = embeddings["intfloat/multilingual-e5-large-instruct"]
    assert e5["revision"] == "274baa43b0e13e37fafa6428dbc7938e62e5c439"
    assert e5["license"] == "MIT"
    assert e5["formatting"] == {
        "document": "{text}",
        "instruction": "Given a web search query, retrieve relevant passages that answer the query",
        "query": "Instruct: {instruction}\nQuery: {text}",
    }
    assert [(item["variant_id"], item["dimensions"]) for item in e5["variants"]] == [
        ("float32-1024d", 1024)
    ]
    assert (e5["benchmark_max_tokens"], e5["native_max_tokens"]) == (512, 512)

    small = embeddings["BAAI/bge-small-en-v1.5"]
    assert small["revision"] == "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    assert small["license"] == "MIT"
    assert small["languages"] == ["EN"]
    assert small["formatting"] == {
        "document": "{text}",
        "instruction": "Represent this sentence for searching relevant passages:",
        "query": "{instruction} {text}",
    }
    assert [(item["variant_id"], item["dimensions"]) for item in small["variants"]] == [
        ("float32-384d", 384)
    ]

    gemma = embeddings["google/embeddinggemma-300m"]
    assert gemma["revision"] == "57c266a740f537b4dc058e1b0cda161fd15afa75"
    assert gemma["license"] == "Gemma"
    assert gemma["shipping_eligible"] is False
    assert gemma["exclusion_reasons"] == ["license_requires_separate_acceptance"]
    assert gemma["formatting"] == {
        "document": "title: none | text: {text}",
        "instruction": "task: search result | query:",
        "query": "{instruction} {text}",
    }
    assert [(item["variant_id"], item["dimensions"]) for item in gemma["variants"]] == [
        ("float32-128d", 128),
        ("float32-256d", 256),
        ("float32-512d", 512),
        ("float32-768d", 768),
    ]

    bge = embeddings["BAAI/bge-m3"]
    assert bge["revision"] == "5617a9f61b028005a4858fdac845db406aefb181"
    assert bge["license"] == "MIT"
    assert bge["formatting"] == {
        "document": "{text}",
        "instruction": None,
        "query": "{text}",
    }
    assert [(item["variant_id"], item["dimensions"]) for item in bge["variants"]] == [
        ("float32-1024d", 1024)
    ]
    assert (bge["benchmark_max_tokens"], bge["native_max_tokens"]) == (512, 8192)

    qwen = embeddings["Qwen/Qwen3-Embedding-0.6B"]
    assert qwen["revision"] == "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    assert qwen["license"] == "Apache-2.0"
    assert qwen["formatting"] == {
        "document": "{text}",
        "instruction": "Given a web search query, retrieve relevant passages that answer the query",
        "query": "Instruct: {instruction}\nQuery:{text}",
    }
    assert [(item["variant_id"], item["dimensions"]) for item in qwen["variants"]] == [
        ("float32-384d", 384),
        ("float32-1024d", 1024),
    ]
    assert (qwen["benchmark_max_tokens"], qwen["native_max_tokens"]) == (
        512,
        32768,
    )


def _reranker_variant_shape(model: dict) -> bool:
    """A reranker ships exactly one float32 variant with no dimensions."""
    if model["kind"] != "reranker":
        return True
    pairs = [(item["variant_id"], item["dimensions"]) for item in model["variants"]]
    return pairs == [("float32", None)]


def _model_variant_violations(model: dict) -> list:
    variant_ids = [variant["variant_id"] for variant in model["variants"]]
    checks = (
        ("variant_id_unique", len(variant_ids) == len(set(variant_ids))),
        ("reranker_variants", _reranker_variant_shape(model)),
    )
    return [name for name, holds in checks if not holds]


def _shipping_language_violations(model: dict) -> list:
    """A shipping model names supported languages and carries no exclusion."""
    if not model["shipping_eligible"]:
        return []
    checks = (
        ("languages_supported", set(model["languages"]) <= TARGET_LANGUAGES),
        ("languages_present", bool(model["languages"])),
        ("no_exclusions", model["exclusion_reasons"] == []),
    )
    return [name for name, holds in checks if not holds]


def test_reranker_pins_depths_and_multilingual_shipping_coverage():
    matrix = _load()
    rerankers = {model["id"]: model for model in matrix["rerankers"]}
    assert matrix["benchmark_contract"]["reranker_depths"] == [10, 20, 50]
    assert {
        model["revision"] for model in rerankers.values()
    } == {
        "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "e61197ed45024b0ed8a2d74b80b4d909f1255473",
    }
    assert set(rerankers) == {
        "BAAI/bge-reranker-v2-m3",
        "Qwen/Qwen3-Reranker-0.6B",
    }
    for model in matrix["embeddings"] + matrix["rerankers"]:
        assert _model_variant_violations(model) == []
        assert _shipping_language_violations(model) == []


def test_inference_and_offline_policy_is_bound_to_every_model():
    matrix = _load()
    contract = matrix["benchmark_contract"]
    assert contract["batch_size"] == 8
    assert contract["precision"] == "float32"
    assert contract["cache_policy"] == {
        "network_access": "prefetch_only",
        "offline_required": True,
        "revision_scoped": True,
    }
    for model in matrix["embeddings"] + matrix["rerankers"]:
        assert model["batch_size"] == 8
        assert model["cache_policy"] == contract["cache_policy"]
        assert all(variant["precision"] == "float32" for variant in model["variants"])


def test_embedding_inference_and_mrl_contracts_are_exact():
    embeddings = {model["id"]: model for model in _load()["embeddings"]}
    assert embeddings["intfloat/multilingual-e5-large-instruct"]["inference"] == {
        "l2_normalize": True,
        "max_length_tokens": 512,
        "padding_side": "right",
        "pooling": "mean",
        "truncation_side": "right",
    }
    assert embeddings["BAAI/bge-m3"]["inference"] == {
        "l2_normalize": True,
        "max_length_tokens": 512,
        "padding_side": "right",
        "pooling": "cls",
        "truncation_side": "right",
    }
    assert embeddings["Qwen/Qwen3-Embedding-0.6B"]["inference"] == {
        "l2_normalize": True,
        "max_length_tokens": 512,
        "padding_side": "left",
        "pooling": "last_token",
        "truncation_side": "right",
    }
    for model in embeddings.values():
        for variant in model["variants"]:
            mrl = variant["mrl"]
            if model["id"] in {"Qwen/Qwen3-Embedding-0.6B", "google/embeddinggemma-300m"}:
                assert mrl == {
                    "enabled": True,
                    "renormalize_after_truncation": True,
                    "truncate_to_dimensions": variant["dimensions"],
                }
            else:
                assert mrl == {
                    "enabled": False,
                    "renormalize_after_truncation": False,
                    "truncate_to_dimensions": None,
                }


def test_reranker_formatting_contracts_are_complete_and_reproducible():
    rerankers = {model["id"]: model for model in _load()["rerankers"]}
    bge = rerankers["BAAI/bge-reranker-v2-m3"]["formatting"]
    assert bge == {
        "assistant_suffix": None,
        "contract_type": "tokenizer_pair_sequence_classification",
        "document_template": "{document}",
        "instruction": None,
        "max_length_tokens": 512,
        "query_template": "{query}",
        "score_tokens": None,
        "scoring": "sequence_classification_logit",
        "system_prefix": None,
        "truncation": "longest_first",
        "user_template": None,
    }
    qwen = rerankers["Qwen/Qwen3-Reranker-0.6B"]["formatting"]
    assert qwen == {
        "assistant_suffix": "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
        "contract_type": "causal_lm_yes_no",
        "document_template": "{document}",
        "instruction": "Given a web search query, retrieve relevant passages that answer the query",
        "max_length_tokens": 512,
        "query_template": "{query}",
        "score_tokens": {"negative": "no", "positive": "yes"},
        "scoring": "softmax_probability_of_yes_over_no",
        "system_prefix": '<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n',
        "truncation": "longest_first_reserving_prefix_and_suffix",
        "user_template": "<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}",
    }


def test_jieba_verified_policy_matches_locked_runtime_constants():
    lexical = _load()["lexical"]
    assert lexical["id"] == "jieba"
    assert lexical["version"] == _runner_literal("JIEBA_VERSION") == "0.42.1"
    assert lexical["license"] == "MIT"
    assert lexical["hmm"] is False
    assert lexical["package_sha256"] == (
        "055ca12f62674fafed09427f176506079bc135638a14e23e25be909131928db2"
    )
    assert lexical["dictionary_sha256"] == _runner_literal(
        "JIEBA_DEFAULT_DICTIONARY_SHA256"
    ) == "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
    assert lexical["dictionary_sha256_status"] == "verified"
    assert lexical["shipping_eligible"] is True
    assert lexical["exclusion_reasons"] == []
    assert lexical["query_document_configuration_identical"] is True
    assert _load()["selection"]["default_embedding"] is None


def test_matrix_contains_no_measurements_or_results_before_real_runs():
    matrix = _load()
    assert matrix["benchmark_contract"]["quality_claims_allowed"] is True
    variants = [
        variant
        for candidate in matrix["embeddings"] + matrix["rerankers"]
        for variant in candidate["variants"]
    ]
    evidence = variants + [matrix["lexical"]]
    for item in evidence:
        _validate_quality(item["quality"])
        _validate_resources(item["resource_measurements"])
        assert item["quality"]["status"] == "unmeasured"
        assert item["quality"]["claim"] is None
        assert item["quality"]["overall"] is None
        assert all(value is None for value in item["quality"]["per_language"].values())
        measurements = item["resource_measurements"]
        assert measurements["status"] == "unmeasured"
        assert all(value is None for key, value in measurements.items() if key != "status")


def test_real_retrieval_benchmark_extra_is_optional_and_locked():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert 'retrieval-benchmark = [' in project
    assert '"sentence-transformers>=5.1,<6"' in project
    assert '"usearch==2.26.0"' in project
    assert '"jieba==0.42.1"' in project
    assert 'name = "usearch"' in lock
    assert 'extra == \'retrieval-benchmark\'' in lock


def test_selection_requires_all_gates_pareto_and_raw_result_evidence():
    selection = _load()["selection"]
    assert selection["baseline"] == {
        "overall_basis_points": 9348,
        "parent_recall_at_10_basis_points": 10000,
        "policy_sha256": "28ef83ac73141b57caf08539b55420a539294c31a7cc152d9e057e88141fa5d7",
        "raw_report_path": "benchmark/baseline-2026-07-16-retrieval.json",
        "raw_report_sha256": "efc07b3f50a529adb3b64ac4733d47888a8a464152b76738f48a119966400a0a",
    }
    assert selection["limits"] == {
        "peak_rss_bytes": 4 * 1024**3,
        "warm_p95_ms": 1000,
    }
    assert selection["material_improvement"] == {
        "metric": "overall",
        "minimum_absolute_gain_basis_points": 100,
    }
    assert selection["required_gates"] == [
        "every_language_gate",
        "latency",
        "license",
        "no_parent_recall_at_10_regression",
        "ram",
    ]
    assert selection["pareto_objectives"] == {
        "index_bytes": "minimize",
        "overall": "maximize",
        "peak_rss_bytes": "minimize",
        "warm_p95_ms": "minimize",
    }
    assert selection["requires_pareto_efficient"] is True
    assert selection["requires_raw_benchmark_result"] is True
    assert selection["selection_target_fields"] == [
        "dimensions",
        "id",
        "revision",
        "variant_id",
    ]
    assert selection["result_evidence_contract"] == {
        "fingerprint_algorithm": "sha256-canonical-json-v1",
        "gate_fields": sorted(GATE_FIELDS),
        "matrix_policy_scope": "policy_with_measurements_reset_and_selection_runtime_cleared",
        "raw_report_path_policy": "normalized_repo_relative_posix_json_under_benchmark_results",
        "required_fields": sorted(EVIDENCE_FIELDS),
        "schema_version": 1,
    }
    assert selection["predetermined_winner"] is False
    assert selection["status"] == "awaiting_raw_benchmark"
    assert selection["result_evidence"] is None
    assert selection["default_embedding"] is None
    assert selection["default_reranker"] is None
    _validate_selection(_load())


def test_default_selection_without_raw_result_evidence_fails_closed():
    matrix = deepcopy(_load())
    matrix["selection"]["default_embedding"] = {
        "dimensions": 1024,
        "id": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "variant_id": "float32-1024d",
    }

    with pytest.raises(AssertionError):
        _validate_selection(matrix)


def test_non_dominated_synthetic_matrix_can_select_without_changing_policy_tests():
    matrix, reports = _synthetic_measured_matrix()

    _validate_selection(matrix, reports)
    assert _matrix_policy_fingerprint(matrix) == _matrix_policy_fingerprint(_load())


def test_result_evidence_rejects_empty_unrelated_stale_and_unqualified_data():
    def rejected(mutator) -> None:
        matrix, reports = _synthetic_measured_matrix()
        mutator(matrix, reports)
        with pytest.raises((AssertionError, KeyError, StopIteration, TypeError, ValueError)):
            _validate_selection(matrix, reports)

    def unrelated_report(matrix: dict, reports: dict[str, bytes]) -> None:
        path = matrix["selection"]["result_evidence"]["raw_report_path"]
        reports[path] = b"{}"
        matrix["selection"]["result_evidence"]["raw_report_sha256"] = _sha256_bytes(
            reports[path]
        )

    rejected(lambda matrix, reports: matrix["selection"].update(result_evidence={}))
    rejected(
        lambda matrix, reports: matrix["selection"]["result_evidence"].update(
            raw_report_path="docs/unrelated.json"
        )
    )
    rejected(
        lambda matrix, reports: matrix["selection"]["result_evidence"].update(
            matrix_policy_sha256="0" * 64
        )
    )
    rejected(
        lambda matrix, reports: matrix["selection"]["result_evidence"].update(
            benchmark_contract_sha256="0" * 64
        )
    )
    rejected(
        lambda matrix, reports: matrix["selection"]["result_evidence"].update(
            benchmark_corpus_sha256="0" * 64
        )
    )
    rejected(
        lambda matrix, reports: matrix["selection"]["result_evidence"].update(
            raw_report_sha256="0" * 64
        )
    )
    rejected(unrelated_report)
    rejected(
        lambda matrix, reports: next(
            variant
            for candidate in matrix["embeddings"]
            for variant in candidate["variants"]
            if variant["variant_id"] == "float32-384d"
            and candidate["id"] == "Qwen/Qwen3-Embedding-0.6B"
        )["quality"].update(
            overall=None,
            per_language={"EN": None, "RU": None, "ZH": None},
            status="unmeasured",
        )
    )
    rejected(
        lambda matrix, reports: next(
            candidate
            for candidate in matrix["embeddings"]
            if candidate["id"] == matrix["selection"]["default_embedding"]["id"]
        ).update(shipping_eligible=False)
    )


def test_selection_rejects_a_target_dominated_by_bound_eligible_evidence():
    matrix, reports = _synthetic_measured_matrix()
    selection = matrix["selection"]
    evidence = selection["result_evidence"]
    selected_item = next(
        item
        for item in evidence["pareto"]["candidates"]
        if item["target"] == selection["default_embedding"]
    )
    competitor = next(
        item
        for item in evidence["pareto"]["candidates"]
        if item["target"] != selection["default_embedding"]
    )
    _, competitor_variant = _resolve_target(matrix, competitor["target"])
    selected_values = selected_item["objective_values"]
    competitor_variant["quality"]["overall"] = selected_values["overall"]
    competitor_variant["resource_measurements"].update(
        index_bytes=selected_values["index_bytes"] - 1,
        peak_rss_bytes=selected_values["peak_rss_bytes"],
        warm_p95_ms=selected_values["warm_p95_ms"],
    )
    competitor["objective_values"] = {
        "index_bytes": competitor_variant["resource_measurements"]["index_bytes"],
        "overall": competitor_variant["quality"]["overall"],
        "peak_rss_bytes": competitor_variant["resource_measurements"]["peak_rss_bytes"],
        "warm_p95_ms": competitor_variant["resource_measurements"]["warm_p95_ms"],
    }
    report_path = evidence["raw_report_path"]
    reports[report_path] = _canonical_bytes(
        {field: evidence[field] for field in RAW_REPORT_FIELDS}
    )
    evidence["raw_report_sha256"] = _sha256_bytes(reports[report_path])

    with pytest.raises(AssertionError):
        _validate_selection(matrix, reports)


def test_measurement_status_transitions_are_typed_and_range_checked():
    matrix = _load()
    lexical = matrix["lexical"]
    _validate_quality(lexical["quality"])
    _validate_resources(lexical["resource_measurements"])

    invalid_quality = deepcopy(lexical["quality"])
    invalid_quality["status"] = "measured"
    with pytest.raises(AssertionError):
        _validate_quality(invalid_quality)

    invalid_quality = deepcopy(lexical["quality"])
    invalid_quality["overall"] = 0.5
    with pytest.raises(AssertionError):
        _validate_quality(invalid_quality)

    invalid_resources = deepcopy(lexical["resource_measurements"])
    invalid_resources["status"] = "measured"
    with pytest.raises(AssertionError):
        _validate_resources(invalid_resources)

    invalid_resources = deepcopy(lexical["resource_measurements"])
    invalid_resources["peak_rss_bytes"] = -1
    invalid_resources["status"] = "measured"
    with pytest.raises(AssertionError):
        _validate_resources(invalid_resources)
