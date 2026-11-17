"""Scanning the declared artifacts and validating their databases."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from llm_wiki import generation_limits
from llm_wiki.generation_descriptors import hash_artifact
from llm_wiki.generation_fields import check_monotonic_deadline
from llm_wiki.generation_limits import (
    ARTIFACT_KEYS,
    MAX_GENERATION_BYTES,
    V2_OPTIONAL_ARTIFACTS,
    V2_REQUIRED_ARTIFACTS,
    VECTOR_FILES,
)
from llm_wiki.generation_manifest import artifact_relative_path, artifact_size, required_sha256
from reliable_memory import validate_runtime_file


class ArtifactScan:
    """Every artifact hashed against the manifest, under the generation bounds."""

    def __init__(self, generation_path: Path, state_root: Path) -> None:
        from verified_artifacts import VerifiedArtifacts

        self.generation_path = generation_path
        self.state_root = state_root
        self.seen: set[str] = set()
        self.normalized: list[dict[str, object]] = []
        self.digests: dict[str, str] = {}
        self.total = 0
        self.verified = VerifiedArtifacts(state_root)

    def add(self, artifact: object, **stop: object) -> None:
        if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_KEYS:
            raise ValueError("each artifact must be a closed object")
        path_text = artifact_relative_path(artifact, self.seen)
        size = self._counted_size(artifact)
        digest = required_sha256(artifact["sha256"], "artifact sha256")
        self._verify(path_text, size, digest, **stop)
        self.seen.add(path_text)
        self.normalized.append({"path": path_text, "size": size, "sha256": digest})

    def _counted_size(self, artifact: dict[str, object]) -> int:
        size = artifact_size(artifact)
        self.total += size
        if self.total > MAX_GENERATION_BYTES:
            raise ValueError("generation artifact bytes exceed the supported bound")
        return size

    def _remembered(self, artifact_path: Path, path_text: str, size: int) -> str | None:
        """The digest already verified for these exact bytes, if any.

        The stat identity is taken through the same runtime validation the hash
        path uses, so a link, a wrong owner or an oversized file is refused here
        exactly as it would be there.
        """
        metadata = validate_runtime_file(artifact_path, self.state_root, max_bytes=size)
        return self.verified.remembered(
            self.generation_path.name, path_text, metadata
        )

    def _hashed(self, artifact_path: Path, path_text: str, size: int, **stop: object) -> str:
        actual_size, actual_digest = hash_artifact(
            artifact_path, self.state_root, size, **stop
        )
        if actual_size != size:
            raise ValueError(f"artifact has wrong size: {path_text}")
        return actual_digest

    def _verified_digest(
        self, artifact_path: Path, path_text: str, size: int, **stop: object
    ) -> str:
        remembered = self._remembered(artifact_path, path_text, size)
        if remembered is not None:
            return remembered
        actual_digest = self._hashed(artifact_path, path_text, size, **stop)
        metadata = validate_runtime_file(artifact_path, self.state_root, max_bytes=size)
        self.verified.remember(
            self.generation_path.name, path_text, metadata, actual_digest
        )
        return actual_digest

    def _verify(self, path_text: str, size: int, digest: str, **stop: object) -> None:
        artifact_path = self.generation_path.joinpath(*PurePosixPath(path_text).parts)
        try:
            actual_digest = self._verified_digest(artifact_path, path_text, size, **stop)
        except FileNotFoundError as error:
            raise ValueError(f"declared artifact is missing: {path_text}") from error
        if actual_digest != digest:
            raise ValueError(f"artifact has wrong hash: {path_text}")
        self.digests[path_text] = actual_digest


def scan_artifacts(
    artifacts: object, generation_path: Path, state_root: Path, **stop: object
) -> ArtifactScan:
    _require_artifact_list(artifacts)
    scan = ArtifactScan(generation_path, state_root)
    for artifact in artifacts:
        check_monotonic_deadline(stop.get("deadline"), stop.get("monotonic", time.monotonic))
        scan.add(artifact, **stop)
    scan.normalized.sort(key=lambda item: str(item["path"]))
    if artifacts != scan.normalized:
        raise ValueError("artifacts must be normalized and sorted by path")
    scan.verified.save()
    return scan


def _require_artifact_list(artifacts: object) -> None:
    if not isinstance(artifacts, list):
        raise TypeError("artifacts must be an array")
    if not 1 <= len(artifacts) <= generation_limits.MAX_ARTIFACTS:
        raise ValueError("artifact count is outside the supported bounds")


def _require_vector_set(
    present_vectors: set[str],
    embedding_present: tuple[bool, bool, bool],
    incomplete: str,
    metadata: str,
) -> None:
    """Both vector files, or neither, and never files without their metadata."""
    if present_vectors != VECTOR_FILES:
        raise ValueError(incomplete)
    if not all(embedding_present):
        raise ValueError(metadata)


def _require_present_vectors(
    present_vectors: set[str], embedding_present: tuple[bool, bool, bool]
) -> None:
    if not present_vectors:
        return
    _require_vector_set(
        present_vectors,
        embedding_present,
        "partial vector artifacts are forbidden",
        "vector artifacts require embedding metadata",
    )


def require_vector_contract(
    vector_state: object, seen: set[str], embedding_present: tuple[bool, bool, bool]
) -> str:
    if vector_state not in {"absent", "complete", "stale"}:
        raise ValueError("vector_state must be absent, complete, or stale")
    present_vectors = seen & VECTOR_FILES
    _require_complete_vectors(vector_state, present_vectors, embedding_present)
    if vector_state == "absent" and (present_vectors or any(embedding_present)):
        raise ValueError("absent vectors must not have vector artifacts or metadata")
    _require_present_vectors(present_vectors, embedding_present)
    return str(vector_state)


def _require_complete_vectors(
    vector_state: object, present_vectors: set[str], embedding_present: tuple[bool, bool, bool]
) -> None:
    if vector_state != "complete":
        return
    _require_vector_set(
        present_vectors,
        embedding_present,
        "complete vectors require vectors.npy and vectors.json",
        "complete vectors require embedding metadata",
    )


def require_schema_contract(
    normalized: dict[str, object], graph_schema: str | None, seen: set[str]
) -> None:
    if normalized["schema_version"] != "corpus-generation/v2":
        return
    _require_v2_contract(normalized, graph_schema, seen)


def _require_v2_artifacts(seen: set[str]) -> None:
    allowed = V2_REQUIRED_ARTIFACTS | V2_OPTIONAL_ARTIFACTS
    if not V2_REQUIRED_ARTIFACTS <= seen or not seen <= allowed:
        raise ValueError("corpus-generation/v2 has an invalid artifact contract")


def _require_v2_contract(
    normalized: dict[str, object], graph_schema: str | None, seen: set[str]
) -> None:
    _require_v2_artifacts(seen)
    if graph_schema != "evidence-graph/v2":
        raise ValueError("corpus-generation/v2 requires the Evidence Graph schema")
    if "repository_scope" not in normalized:
        raise ValueError("corpus-generation/v2 requires a validated repository scope")


def validate_artifact_databases(
    generation_path: Path,
    normalized: dict[str, object],
    graph_schema: str | None,
    state_root: Path,
    *,
    deep: bool = True,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    from evidence_graph import validate_generation_artifact

    if normalized["schema_version"] == "corpus-generation/v2":
        from generation_fts_validation import (
            validate_generation_fts_artifact,
        )

        validate_generation_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        validate_generation_fts_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deep=deep,
            deadline=deadline,
            cancelled=cancelled,
        )
        return
    if graph_schema == "evidence-graph/v2":
        validate_generation_artifact(
            generation_path,
            normalized,
            state_root=state_root,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
