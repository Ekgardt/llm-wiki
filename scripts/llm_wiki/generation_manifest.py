"""Normalizing a generation manifest and the artifacts it declares."""

from __future__ import annotations

import json

from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.generation_fields import bounded_version, checked_generation_id, optional_version
from llm_wiki.generation_limits import (
    MANIFEST_KEYS,
    MAX_ARTIFACT_BYTES,
    REQUIRED_MANIFEST_KEYS,
    SHA256_RE,
)
from llm_wiki.restricted_paths import restricted_relative_path
from repository_scope import RepositoryScope


def manifest_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest.json must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("generation manifest must be an object")
    return value


def _require_manifest_keys(value: dict[str, object]) -> None:
    keys = set(value)
    if not REQUIRED_MANIFEST_KEYS <= keys or not keys <= MANIFEST_KEYS:
        raise ValueError("generation manifest has missing or unknown properties")


def _require_manifest_shape(value: dict[str, object], raw: bytes, expected_id: str) -> None:
    _require_manifest_keys(value)
    if canonical_json_bytes(value) != raw:
        raise ValueError("generation manifest must use canonical JSON")
    if checked_generation_id(str(value["generation_id"])) != expected_id:
        raise ValueError("generation_id must equal the generation directory name")


def _manifest_parent(value: dict[str, object], expected_id: str) -> str | None:
    parent = value.get("parent_generation_id")
    if parent is None:
        return None
    parent = checked_generation_id(str(parent))
    if parent == expected_id:
        raise ValueError("generation cannot be its own parent")
    return parent


def _normalized_versions(value: dict[str, object], expected_id: str) -> dict[str, object]:
    return {
        "generation_id": expected_id,
        "schema_version": bounded_version("schema_version", value["schema_version"]),
        "collector_version": bounded_version("collector_version", value["collector_version"]),
        "extractor_version": bounded_version("extractor_version", value["extractor_version"]),
        "tokenizer_version": bounded_version("tokenizer_version", value["tokenizer_version"]),
    }


def required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _bounded_dimension(dimensions: object) -> bool:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        return False
    return 1 <= dimensions <= 65536


def _validated_dimensions(dimensions: object) -> int | None:
    if dimensions is None:
        return None
    if not _bounded_dimension(dimensions):
        raise ValueError("vector_dimensions must be null or a positive bounded integer")
    return dimensions


def _embedding_fields(
    value: dict[str, object]
) -> tuple[str | None, str | None, int | None, tuple[bool, bool, bool]]:
    embedding_id = optional_version("embedding_model_id", value["embedding_model_id"])
    embedding_revision = optional_version(
        "embedding_model_revision", value["embedding_model_revision"]
    )
    dimensions = _validated_dimensions(value["vector_dimensions"])
    present = (
        embedding_id is not None,
        embedding_revision is not None,
        dimensions is not None,
    )
    if any(present) and not all(present):
        raise ValueError("embedding model ID, revision, and dimensions must be all set or null")
    return embedding_id, embedding_revision, dimensions, present


def _graph_fields(value: dict[str, object]) -> tuple[str | None, str | None]:
    graph_schema = optional_version("graph_schema_version", value["graph_schema_version"])
    graph_extractor = optional_version(
        "graph_extractor_version", value["graph_extractor_version"]
    )
    if graph_schema not in {None, "evidence-graph/v2"}:
        raise ValueError("graph schema must be null or evidence-graph/v2")
    if (graph_schema is None) != (graph_extractor is None):
        raise ValueError("graph schema and extractor versions must be both set or null")
    return graph_schema, graph_extractor


def _normalized_parent_section(_value: dict[str, object], parent: str | None) -> object:
    return parent


def _normalized_scope_section(value: dict[str, object], _parent: str | None) -> object:
    return RepositoryScope.from_dict(value["repository_scope"]).as_dict()


def _normalized_code_roots_section(value: dict[str, object], _parent: str | None) -> object:
    roots = value["code_roots"]
    if not isinstance(roots, list) or not all(isinstance(name, str) for name in roots):
        raise ValueError("code_roots must be an array of strings")
    if not roots:
        raise ValueError("code_roots must name at least one root when present")
    return list(roots)


_OPTIONAL_MANIFEST_SECTIONS = {
    "parent_generation_id": _normalized_parent_section,
    "repository_scope": _normalized_scope_section,
    "code_roots": _normalized_code_roots_section,
}


def _apply_optional_sections(
    normalized: dict[str, object], value: dict[str, object], parent: str | None
) -> None:
    for key, normalize in _OPTIONAL_MANIFEST_SECTIONS.items():
        if key in value:
            normalized[key] = normalize(value, parent)


def artifact_relative_path(artifact: dict[str, object], seen: set[str]) -> str:
    path_value = artifact["path"]
    if not isinstance(path_value, str):
        raise TypeError("artifact path must be a string")
    first = path_value.split("/", 1)[0]
    path_text = restricted_relative_path(path_value, (first,)).as_posix()
    if path_text == "manifest.json" or path_text in seen:
        raise ValueError("artifact paths must be unique and exclude manifest.json")
    return path_text


def artifact_size(artifact: dict[str, object]) -> int:
    size = artifact["size"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("artifact size is outside the supported bounds")
    if not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise ValueError("artifact size is outside the supported bounds")
    return size


def normalized_manifest(
    value: dict[str, object], raw: bytes, expected_id: str
) -> tuple[dict[str, object], tuple[bool, bool, bool], str | None]:
    """The manifest as the catalog stores it, with what the contracts need."""
    _require_manifest_shape(value, raw, expected_id)
    parent = _manifest_parent(value, expected_id)
    normalized = _normalized_versions(value, expected_id)
    normalized["tokenizer_config_sha256"] = required_sha256(
        value["tokenizer_config_sha256"], "tokenizer_config_sha256"
    )
    embedding_id, embedding_revision, dimensions, present = _embedding_fields(value)
    normalized["embedding_model_id"] = embedding_id
    normalized["embedding_model_revision"] = embedding_revision
    normalized["vector_dimensions"] = dimensions
    graph_schema, graph_extractor = _graph_fields(value)
    normalized["graph_schema_version"] = graph_schema
    normalized["graph_extractor_version"] = graph_extractor
    _apply_optional_sections(normalized, value, parent)
    normalized["source_manifest_sha256"] = required_sha256(
        value["source_manifest_sha256"], "source_manifest_sha256"
    )
    return normalized, present, graph_schema
