"""Task 19: atomic full Evidence Graph generation builder.

Builds an unpublished generation directory under
``<state_root>/cache/evidence-graph/generations/<generation-id>/`` while
existing readers continue using the prior active generation. The build
pipeline is:

  1. snapshot source membership and exact source SHA-256 hashes
  2. create the generation directory
  3. write ``evidence.sqlite3`` via the immutable hard-link path
  4. write canonical ``source-manifest.json`` and ``manifest.json``
  5. validate schema, foreign keys, ``PRAGMA integrity_check``, evidence
     spans, artifact hashes, and source membership
  6. register the generation in the shared catalog
  7. compare-and-swap activate against ``expected_active`` in one short
     catalog transaction

Six named kill points support deterministic crash-testing at every
boundary. The builder NEVER mutates an active generation in place; on any
failure before activation, the active pointer is unchanged and the prior
generation remains fully readable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import corpus_snapshot
import evidence_graph
import generation_catalog
from code_intelligence import VerifiedAnalysisBatch
from reliable_memory import canonical_json_bytes, fsync_directory, fsync_file, read_runtime_bytes
from repository_scope import RepositoryScope, same_repository_record

GRAPH_SCHEMA_VERSION = evidence_graph.GRAPH_SCHEMA_VERSION
DEFAULT_GRAPH_EXTRACTOR_VERSION = "graph-extractor/v1"
DEFAULT_TOKENIZER_VERSION = "tokenizer/v1"
DEFAULT_TOKENIZER_CONFIG_SHA256 = "0" * 64
CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v1"
COMPLETE_CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v2"
INCREMENTAL_MANIFEST_VERSION = "evidence-graph-incremental/v5"
_LEGACY_INCREMENTAL_MANIFEST_VERSIONS = frozenset(
    {
        "evidence-graph-incremental/v1",
        "evidence-graph-incremental/v2",
        "evidence-graph-incremental/v3",
        "evidence-graph-incremental/v4",
    }
)
#: Versions whose source entries carry `workspace_sensitive` per entry.
_WORKSPACE_SENSITIVE_VERSIONS = frozenset(
    {"evidence-graph-incremental/v4", "evidence-graph-incremental/v5"}
)
# Up to v4 a 64 MiB constant stood here in both roles, and it was the whole
# defect. The manifest carried one `record_dependencies` row per record, so on
# this vault's corpus it reached 158,075,010 bytes against 349,306 records, was
# silently dropped, and no generation on disk ever carried one — reuse could not
# happen for anybody. A bound on a quantity that grows with the corpus can only
# ever be outgrown, so neither of these is that bound any more.
#
# Reading is bounded by the size the sealed `manifest.json` declares for this
# artifact, which `generation_catalog._validate_generation` has already verified
# by hashing it — see `_declared_manifest_bytes`. What is left here is the
# absurdity ceiling the catalog itself applies to any one artifact: a manifest
# past it could not be registered as a generation artifact at all.
MAX_INCREMENTAL_MANIFEST_BYTES = generation_catalog.MAX_ARTIFACT_BYTES
MAX_STORED_INCREMENTAL_MANIFEST_BYTES = MAX_INCREMENTAL_MANIFEST_BYTES
#: `record_dependencies` is audit provenance that no reader in `scripts/`
#: consumes and that is fully derivable from `sources` plus the membership
#: sidecar. It stays in the manifest for a human and for the tests that read it,
#: but materialising all 349,306 rows is what made the manifest unstorable, so
#: it is a deterministic prefix and the manifest states the true total.
MAX_INLINE_RECORD_DEPENDENCY_ROWS = 10_000
MAX_LEGACY_WORKSPACE_SENSITIVE_SOURCES = 10_000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INVALIDATION_KEYS = frozenset(
    {"exports", "imports", "signatures", "aliases", "project_metadata"}
)
_RECORD_COLLECTIONS = (
    "nodes",
    "occurrences",
    "assertions",
    "evidence",
    "observations",
    "dependencies",
)
_RECORD_KEYS = {
    "nodes": "node_id",
    "occurrences": "occurrence_id",
    "assertions": "assertion_id",
    "evidence": "evidence_id",
    "observations": "observation_id",
    "dependencies": "dependency_id",
}

KILL_POINTS: tuple[str, ...] = (
    "before_directory_create",
    "during_extraction",
    "after_database_commit",
    "after_validation",
    "before_activation",
    "after_activation",
)
KillPoint = Literal[
    "before_directory_create",
    "during_extraction",
    "after_database_commit",
    "after_validation",
    "before_activation",
    "after_activation",
]

_DEFAULT_POLICY: Mapping[str, object] = {
    "daily_paths": (),
    "code_roots": (),
    "include_historical": False,
    "as_of": None,
}


class KillPointError(RuntimeError):
    """Raised at a configured kill point to abort a build deliberately.

    Tests catch this to verify that the active generation is still
    readable and that any orphaned partial directory is ignored on the
    next catalog start.
    """

    def __init__(self, kill_point: str) -> None:
        super().__init__(f"kill point: {kill_point}")
        self.kill_point = kill_point


@dataclass(frozen=True)
class BuildResult:
    """Outcome of a successful (or partially successful) build."""

    generation_id: str
    generation_path: Path
    manifest: Mapping[str, object]
    activated: bool


@dataclass(frozen=True, slots=True)
class IncrementalReuseConfig:
    """Exact toolchain and workspace identity required for record reuse."""

    extractor_version: str
    grammar_version: str
    compiler_version: str
    resolver_config_sha256: str
    schema_version: str
    workspace_manifest_sha256: str

    def __post_init__(self) -> None:
        for name in _BOUNDED_STRING_FIELDS:
            _require_bounded_string(getattr(self, name), name)
        for name in _DIGEST_FIELDS:
            _require_digest(getattr(self, name), name)


_BOUNDED_STRING_FIELDS = (
    "extractor_version",
    "grammar_version",
    "compiler_version",
    "schema_version",
)

_DIGEST_FIELDS = ("resolver_config_sha256", "workspace_manifest_sha256")


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_bounded_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{name} must be a bounded non-empty string")


def _require_digest(value: object, name: str) -> None:
    if not _is_digest(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class SourceExtraction:
    """Complete records and invalidation metadata produced for one source."""

    nodes: tuple[Mapping[str, object], ...] = ()
    occurrences: tuple[Mapping[str, object], ...] = ()
    assertions: tuple[Mapping[str, object], ...] = ()
    evidence: tuple[Mapping[str, object], ...] = ()
    observations: tuple[Mapping[str, object], ...] = ()
    dependencies: tuple[Mapping[str, object], ...] = ()
    source_dependencies: tuple[str, ...] = ()
    workspace_sensitive: bool = False
    invalidation_fingerprints: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class IncrementalBuildResult(BuildResult):
    """Published generation plus the exact source delta and reuse decision."""

    added_sources: tuple[str, ...]
    changed_sources: tuple[str, ...]
    deleted_sources: tuple[str, ...]
    renamed_sources: tuple[tuple[str, str], ...]
    reused_sources: tuple[str, ...]
    rebuilt_sources: tuple[str, ...]


def _require_type(value: object, expected: type, message: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(message)


def _require_optional_type(value: object, expected: type, message: str) -> None:
    if value is None:
        return
    _require_type(value, expected, message)


def _require_build_types(
    catalog: object,
    graph_schema: object,
    repository_scope: object,
    snapshot: object,
    code_capture: object,
) -> None:
    _require_type(
        catalog,
        generation_catalog.GenerationCatalog,
        "catalog must be a GenerationCatalog",
    )
    _require_type(
        graph_schema, evidence_graph.GraphSchema, "graph_schema must be a GraphSchema"
    )
    _require_optional_type(
        repository_scope,
        RepositoryScope,
        "repository_scope must be a RepositoryScope or None",
    )
    _require_optional_type(
        snapshot,
        corpus_snapshot.CorpusSnapshot,
        "snapshot must be a CorpusSnapshot or None",
    )
    _require_optional_type(
        code_capture,
        corpus_snapshot.CodeCaptureContract,
        "code_capture must be a CodeCaptureContract or None",
    )


def _require_capture_agreement(snapshot: object, code_capture: object) -> None:
    if code_capture is None:
        return
    from code_workspace import code_capture_as_dict, validate_code_capture

    validate_code_capture(code_capture_as_dict(code_capture))
    if snapshot is not None and snapshot.code_capture != code_capture:
        raise ValueError("code_capture must be the exact supplied CorpusSnapshot contract")


def _require_publication_root(
    snapshot: object, activate: bool, publication_root: object
) -> None:
    if snapshot is None or not activate:
        return
    if publication_root is None:
        raise ValueError("complete generation activation requires publication_root")


def _require_v3_inputs(
    graph_schema: object,
    snapshot: object,
    repository_scope: object,
    code_capture: object,
) -> None:
    if graph_schema is not evidence_graph.GraphSchema.V3:
        return
    if snapshot is None or repository_scope is None or code_capture is None:
        raise ValueError(
            "evidence-graph/v3 requires a CorpusSnapshot, repository scope, and code_capture"
        )


def _require_complete_generation_inputs(
    graph_schema: object,
    snapshot: object,
    repository_scope: object,
    code_capture: object,
    activate: bool,
    publication_root: object,
) -> None:
    _require_publication_root(snapshot, activate, publication_root)
    _require_v3_inputs(graph_schema, snapshot, repository_scope, code_capture)


def _validated_build_inputs(
    *,
    catalog: object,
    graph_schema: object,
    repository_scope: RepositoryScope | None,
    snapshot: object,
    code_capture: object,
    activate: bool,
    publication_root: object,
) -> RepositoryScope | None:
    """Check every argument once, and hand back the scope the build will use."""
    _require_build_types(catalog, graph_schema, repository_scope, snapshot, code_capture)
    _require_capture_agreement(snapshot, code_capture)
    _require_complete_generation_inputs(
        graph_schema, snapshot, repository_scope, code_capture, activate, publication_root
    )
    if repository_scope is None:
        return None
    return RepositoryScope.from_dict(repository_scope.as_dict())


def _require_capture_membership(
    code_capture: corpus_snapshot.CodeCaptureContract | None,
    sources_list: list[Mapping[str, object]],
) -> None:
    if code_capture is None:
        return
    capture_membership = sorted(
        (item.source_id, item.relative_path, item.sha256, item.stat.size)
        for item in code_capture.files
    )
    source_membership = sorted(
        (
            str(source["source_id"]),
            str(source["relative_path"]),
            str(source["sha256"]),
            int(source["size"]),
        )
        for source in sources_list
    )
    if capture_membership != source_membership:
        raise ValueError("code_capture files must match builder source membership")


def _snapshot_source_rows(snapshot: corpus_snapshot.CorpusSnapshot) -> list[dict]:
    return [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]


def _require_snapshot_agreement(
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    sources_list: list[Mapping[str, object]],
    source_bytes_snapshot: Mapping[str, bytes],
    *,
    collector_version: str,
    extractor_version: str,
) -> None:
    if snapshot is None:
        return
    if sources_list != _snapshot_source_rows(
        snapshot
    ) or source_bytes_snapshot != _snapshot_content_bytes(snapshot):
        raise ValueError("builder sources must be the exact supplied CorpusSnapshot")
    _require_snapshot_provenance(snapshot, collector_version, extractor_version)


def _snapshot_content_bytes(
    snapshot: corpus_snapshot.CorpusSnapshot,
) -> dict[str, bytes]:
    return {source.record.logical_id: source.content for source in snapshot.sources}


def _require_snapshot_provenance(
    snapshot: corpus_snapshot.CorpusSnapshot,
    collector_version: str,
    extractor_version: str,
) -> None:
    if collector_version != snapshot.collector_version:
        raise ValueError("corpus provenance must match the supplied CorpusSnapshot")
    if extractor_version != snapshot.extractor_version:
        raise ValueError("corpus provenance must match the supplied CorpusSnapshot")


def _generation_source_manifest(
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    sources_list: list[Mapping[str, object]],
    *,
    policy: Mapping[str, object] | None,
    collector_version: str,
    extractor_version: str,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
):
    """Pin source membership and hashes before any extraction can drift them."""
    if snapshot is None:
        return _snapshot_source_manifest(
            sources_list,
            policy=policy,
            collector_version=collector_version,
            extractor_version=extractor_version,
        )
    source_manifest = corpus_snapshot.canonical_source_manifest(
        (source.record for source in snapshot.sources),
        snapshot.policy,
        collector_version=snapshot.collector_version,
        extractor_version=snapshot.extractor_version,
    )
    source_manifest_bytes = canonical_json_bytes(source_manifest)
    source_manifest_sha256 = _hash_bytes(
        source_manifest_bytes, deadline=deadline, cancelled=cancelled
    )
    if source_manifest_sha256 != snapshot.corpus_sha256:
        raise ValueError("CorpusSnapshot hash does not match its canonical source manifest")
    return source_manifest, source_manifest_bytes, source_manifest_sha256


def _require_analysis_batch(
    batch: object,
    count: int,
    source_manifest_sha256: str,
    graph_schema: object,
    repository_scope: RepositoryScope | None,
) -> None:
    _require_analysis_identity(batch, count)
    if batch.source_manifest_sha256 != source_manifest_sha256:
        raise ValueError("verified analysis source manifest must match generation manifest")
    _require_analysis_scope(batch, graph_schema, repository_scope)


def _require_analysis_identity(batch: object, count: int) -> None:
    if count >= evidence_graph.MAX_VALIDATION_ROWS:
        raise ValueError("verified analysis row ceiling exceeded")
    if type(batch) is not VerifiedAnalysisBatch:
        raise TypeError("verified_analyses must contain VerifiedAnalysisBatch values")


def _require_analysis_scope(
    batch: VerifiedAnalysisBatch,
    graph_schema: object,
    repository_scope: RepositoryScope | None,
) -> None:
    if graph_schema is not evidence_graph.GraphSchema.V3:
        return
    observed = (batch.analysis.run.repository_id, batch.analysis.run.checkout_id)
    if observed != (repository_scope.repository_id, repository_scope.checkout_id):
        raise ValueError("verified analysis repository or checkout does not match publication")


def _validated_analysis_batches(
    verified_analyses: Iterable[VerifiedAnalysisBatch],
    *,
    source_manifest_sha256: str,
    graph_schema: object,
    repository_scope: RepositoryScope | None,
    code_capture: object,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[VerifiedAnalysisBatch]:
    validated: list[VerifiedAnalysisBatch] = []
    for batch in verified_analyses:
        _check_stop(deadline, cancelled)
        _require_analysis_batch(
            batch, len(validated), source_manifest_sha256, graph_schema, repository_scope
        )
        validated.append(batch)
    if validated and code_capture is None:
        raise ValueError("verified analyses require code_capture")
    return validated


def _materialized_graph_records(
    *,
    nodes: Iterable[Mapping[str, object]],
    occurrences: Iterable[Mapping[str, object]],
    assertions: Iterable[Mapping[str, object]],
    evidence: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    dependencies: Iterable[Mapping[str, object]],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, list[Mapping[str, object]]]:
    supplied = {
        "nodes": nodes,
        "occurrences": occurrences,
        "assertions": assertions,
        "evidence": evidence,
        "observations": observations,
        "dependencies": dependencies,
    }
    return {
        label: _materialize(
            records,
            label=label,
            limit=evidence_graph.MAX_VALIDATION_ROWS,
            deadline=deadline,
            cancelled=cancelled,
        )
        for label, records in supplied.items()
    }


def _created_generation_directory(
    catalog: generation_catalog.GenerationCatalog, generation_id: str
) -> Path:
    generation_path = catalog.generations_path / generation_id
    if generation_path.exists() or generation_path.is_symlink():
        raise FileExistsError(
            f"generation {generation_id!r} already exists; builder never mutates"
        )
    generation_path.mkdir(parents=True, exist_ok=False)
    fsync_directory(generation_path)
    return generation_path


def _validate_kill_point(kill_point: str | None) -> None:
    if kill_point is not None and kill_point not in KILL_POINTS:
        raise ValueError(f"kill_point must be one of {KILL_POINTS} or None, got {kill_point!r}")


def _shared_sources(
    sources: Iterable[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [
        {
            "logical_id": str(source["source_id"]),
            "relative_path": str(source["relative_path"]),
            "sha256": str(source["sha256"]),
        }
        for source in sources
    ]


def _build_manifest(
    *,
    generation_id: str,
    parent_generation_id: str | None,
    collector_version: str,
    extractor_version: str,
    graph_extractor_version: str,
    source_manifest_sha256: str,
    database_size: int,
    database_sha256: str,
    source_manifest_bytes: bytes,
    repository_scope: RepositoryScope | None,
    graph_schema: evidence_graph.GraphSchema = evidence_graph.GraphSchema.V2,
    schema_version: str = CORPUS_GENERATION_SCHEMA_VERSION,
    tokenizer_version: str = DEFAULT_TOKENIZER_VERSION,
    tokenizer_config_sha256: str = DEFAULT_TOKENIZER_CONFIG_SHA256,
    search_artifact: Mapping[str, object] | None = None,
    incremental_manifest_bytes: bytes | None = None,
    code_capture: corpus_snapshot.CodeCaptureContract | None = None,
    vectors: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    if graph_schema is evidence_graph.GraphSchema.V3 and code_capture is None:
        raise ValueError("evidence-graph/v3 manifests require code_capture")
    manifest = {
        "generation_id": generation_id,
        "schema_version": schema_version,
        "collector_version": collector_version,
        "extractor_version": extractor_version,
        "tokenizer_version": tokenizer_version,
        "tokenizer_config_sha256": tokenizer_config_sha256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": graph_schema.value,
        "graph_extractor_version": graph_extractor_version,
        "source_manifest_sha256": source_manifest_sha256,
        "artifacts": _manifest_artifacts(
            database_size=database_size,
            database_sha256=database_sha256,
            source_manifest_bytes=source_manifest_bytes,
            incremental_manifest_bytes=incremental_manifest_bytes,
            search_artifact=search_artifact,
            vectors=vectors,
        ),
        "vector_state": "absent",
    }
    manifest.update(
        _optional_manifest_fields(parent_generation_id, repository_scope, code_capture)
    )
    manifest.update(_vector_manifest_fields(vectors))
    return manifest


def _vector_manifest_fields(vectors: Mapping[str, object] | None) -> dict[str, object]:
    """A generation declares complete vectors only when it carries them."""
    if vectors is None:
        return {}
    return {
        "embedding_model_id": vectors["model_id"],
        "embedding_model_revision": vectors["model_revision"],
        "vector_dimensions": vectors["dimensions"],
        "vector_state": "complete",
    }


def _manifest_artifacts(
    *,
    database_size: int,
    database_sha256: str,
    source_manifest_bytes: bytes,
    incremental_manifest_bytes: bytes | None,
    search_artifact: Mapping[str, object] | None,
    vectors: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    artifacts = [
        {
            "path": "evidence.sqlite3",
            "size": database_size,
            "sha256": database_sha256,
        },
        {
            "path": "source-manifest.json",
            "size": len(source_manifest_bytes),
            "sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
        },
    ]
    artifacts.extend(
        _optional_artifacts(incremental_manifest_bytes, search_artifact, vectors)
    )
    artifacts.sort(key=lambda item: str(item["path"]))
    return artifacts


def _incremental_manifest_artifacts(
    incremental_manifest_bytes: bytes | None,
) -> list[dict[str, object]]:
    if incremental_manifest_bytes is None:
        return []
    return [
        {
            "path": "incremental-manifest.json",
            "size": len(incremental_manifest_bytes),
            "sha256": hashlib.sha256(incremental_manifest_bytes).hexdigest(),
        }
    ]


def _optional_artifacts(
    incremental_manifest_bytes: bytes | None,
    search_artifact: Mapping[str, object] | None,
    vectors: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Everything a generation carries only when the build produced it."""
    optional = _incremental_manifest_artifacts(incremental_manifest_bytes)
    if search_artifact is not None:
        optional.append(dict(search_artifact))
    if vectors is not None:
        optional.extend(dict(item) for item in vectors["artifacts"])
    return optional


def _optional_manifest_fields(
    parent_generation_id: str | None,
    repository_scope: RepositoryScope | None,
    code_capture: corpus_snapshot.CodeCaptureContract | None,
) -> dict[str, object]:
    """The fields a manifest carries only when the build actually has them."""
    fields: dict[str, object] = {}
    if parent_generation_id:
        fields["parent_generation_id"] = parent_generation_id
    if repository_scope:
        fields["repository_scope"] = repository_scope.as_dict()
    fields.update(_code_capture_field(code_capture))
    return fields


def _code_capture_field(
    code_capture: corpus_snapshot.CodeCaptureContract | None,
) -> dict[str, object]:
    if code_capture is None:
        return {}
    from code_workspace import code_capture_as_dict

    return {"code_capture": code_capture_as_dict(code_capture)}


def _invalid_deadline(deadline: float | None) -> bool:
    if deadline is None:
        return False
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return True
    return not math.isfinite(deadline)


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _check_stop(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    if _invalid_deadline(deadline):
        raise ValueError("deadline must be a finite monotonic timestamp")
    _raise_if_stopped(deadline, cancelled)


def _raise_if_stopped(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> None:
    """Cancellation is asked first: it is the answer the caller already knows."""
    if bool(cancelled and cancelled()):
        raise TimeoutError("Evidence Graph build cancelled")
    if _deadline_reached(deadline):
        raise TimeoutError("Evidence Graph build deadline reached")


def _materialize(
    records: Iterable[Mapping[str, object]],
    *,
    label: str,
    limit: int,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> list[Mapping[str, object]]:
    materialized: list[Mapping[str, object]] = []
    iterator = iter(records)
    while True:
        _check_stop(deadline, cancelled)
        try:
            record = next(iterator)
        except StopIteration:
            break
        if len(materialized) >= limit:
            raise ValueError(f"{label} row ceiling exceeded")
        materialized.append(record)
    _check_stop(deadline, cancelled)

    return materialized


def _hash_bytes(
    content: bytes,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(content), generation_catalog.HASH_CHUNK_BYTES):
        _check_stop(deadline, cancelled)
        digest.update(content[offset : offset + generation_catalog.HASH_CHUNK_BYTES])
    _check_stop(deadline, cancelled)
    return digest.hexdigest()


def _verify_source_snapshot(
    sources: list[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, bytes]:
    identifiers = {source.get("source_id") for source in sources}
    if set(source_bytes) != identifiers:
        raise ValueError("source_bytes must bind every captured source exactly once")
    snapshot: dict[str, bytes] = {}
    for source in sources:
        _check_stop(deadline, cancelled)
        source_id = source.get("source_id")
        content = source_bytes[source_id]
        _require_captured_content(source, content, deadline, cancelled)
        snapshot[source_id] = content
    return snapshot


def _require_captured_content(
    source: Mapping[str, object],
    content: object,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    if not isinstance(content, bytes):
        raise TypeError("captured source content must be bytes")
    # Short-circuit keeps the old order and skips hashing a source whose size
    # already disagrees; both halves raise the same refusal, as they always did.
    if source.get("size") != len(content) or source.get("sha256") != _hash_bytes(
        content, deadline=deadline, cancelled=cancelled
    ):
        raise ValueError("captured source size or hash does not match source bytes")


def _hash_file(
    path: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            _check_stop(deadline, cancelled)
            chunk = handle.read(generation_catalog.HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    _check_stop(deadline, cancelled)
    return total, digest.hexdigest()


def _write_canonical_file(
    path: Path,
    payload: Mapping[str, object],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    encoded = canonical_json_bytes(payload)
    with path.open("xb") as handle:
        for offset in range(0, len(encoded), generation_catalog.HASH_CHUNK_BYTES):
            _check_stop(deadline, cancelled)
            handle.write(encoded[offset : offset + generation_catalog.HASH_CHUNK_BYTES])
        handle.flush()
        _check_stop(deadline, cancelled)
    fsync_file(path)
    _check_stop(deadline, cancelled)


def _snapshot_source_manifest(
    sources: Iterable[Mapping[str, object]],
    *,
    policy: Mapping[str, object] | None,
    collector_version: str,
    extractor_version: str,
) -> tuple[Mapping[str, object], bytes, str]:
    shared = _shared_sources(sources)
    manifest_policy = dict(policy) if policy is not None else dict(_DEFAULT_POLICY)
    manifest_policy.setdefault("daily_paths", ())
    manifest_policy.setdefault("code_roots", ())
    manifest_policy.setdefault("include_historical", False)
    manifest_policy.setdefault("as_of", None)
    source_manifest = corpus_snapshot.canonical_source_manifest(
        shared,
        manifest_policy,
        collector_version=collector_version,
        extractor_version=extractor_version,
    )
    encoded = canonical_json_bytes(source_manifest)
    import hashlib

    return source_manifest, encoded, hashlib.sha256(encoded).hexdigest()


def build_full_generation(
    catalog: generation_catalog.GenerationCatalog,
    *,
    sources: Iterable[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    nodes: Iterable[Mapping[str, object]],
    occurrences: Iterable[Mapping[str, object]],
    assertions: Iterable[Mapping[str, object]],
    evidence: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    dependencies: Iterable[Mapping[str, object]],
    graph_schema: evidence_graph.GraphSchema = evidence_graph.GraphSchema.V2,
    verified_analyses: Iterable[VerifiedAnalysisBatch] = (),
    generation_id: str,
    parent_generation_id: str | None = None,
    policy: Mapping[str, object] | None = None,
    collector_version: str = corpus_snapshot.COLLECTOR_VERSION,
    extractor_version: str = corpus_snapshot.EXTRACTOR_VERSION,
    graph_extractor_version: str = DEFAULT_GRAPH_EXTRACTOR_VERSION,
    expected_active: str | None = None,
    activate: bool = True,
    kill_point: str | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    incremental_manifest: Mapping[str, object] | None = None,
    repository_scope: RepositoryScope | None = None,
    snapshot: corpus_snapshot.CorpusSnapshot | None = None,
    publication_root: Path | None = None,
    coordinator: object | None = None,
    code_capture: corpus_snapshot.CodeCaptureContract | None = None,
) -> BuildResult:
    """Atomically build one full Evidence Graph generation.

    The build is published only after every artifact has been written,
    fsynced, validated, and registered. The active generation pointer
    advances in a single compare-and-swap transaction against
    ``expected_active``. On any failure (including a configured
    ``kill_point``) the active pointer is unchanged and the prior
    generation remains fully readable.

    The function never modifies an active generation in place; if
    ``generation_id`` already exists on disk, the call fails before
    touching anything.
    """
    _validate_kill_point(kill_point)
    repository_scope = _validated_build_inputs(
        catalog=catalog,
        graph_schema=graph_schema,
        repository_scope=repository_scope,
        snapshot=snapshot,
        code_capture=code_capture,
        activate=activate,
        publication_root=publication_root,
    )
    sources_list = [
        dict(source)
        for source in _materialize(
            sources,
            label="sources",
            limit=evidence_graph.MAX_VALIDATION_ROWS,
            deadline=deadline,
            cancelled=cancelled,
        )
    ]
    source_bytes_snapshot = _verify_source_snapshot(
        sources_list, source_bytes, deadline=deadline, cancelled=cancelled
    )
    _require_capture_membership(code_capture, sources_list)
    _require_snapshot_agreement(
        snapshot,
        sources_list,
        source_bytes_snapshot,
        collector_version=collector_version,
        extractor_version=extractor_version,
    )
    source_manifest, source_manifest_bytes, source_manifest_sha256 = (
        _generation_source_manifest(
            snapshot,
            sources_list,
            policy=policy,
            collector_version=collector_version,
            extractor_version=extractor_version,
            deadline=deadline,
            cancelled=cancelled,
        )
    )
    _check_stop(deadline, cancelled)
    verified_analysis_list = _validated_analysis_batches(
        verified_analyses,
        source_manifest_sha256=source_manifest_sha256,
        graph_schema=graph_schema,
        repository_scope=repository_scope,
        code_capture=code_capture,
        deadline=deadline,
        cancelled=cancelled,
    )
    records = _materialized_graph_records(
        nodes=nodes,
        occurrences=occurrences,
        assertions=assertions,
        evidence=evidence,
        observations=observations,
        dependencies=dependencies,
        deadline=deadline,
        cancelled=cancelled,
    )
    _kill_if(kill_point, "before_directory_create")

    generation_path = _created_generation_directory(catalog, generation_id)

    # Track whether we've reached the registration phase. Kill-point aborts
    # leave partial state on disk (they simulate a crash); any other
    # exception during artifact build cleans up so retries are not blocked.
    state = {"publication_attempted": False}
    try:
        _kill_if(kill_point, "during_extraction")
        database_path = generation_path / "evidence.sqlite3"
        _write_generation_database(
            database_path,
            graph_schema=graph_schema,
            sources_list=sources_list,
            source_bytes_snapshot=source_bytes_snapshot,
            records=records,
            verified_analysis_list=verified_analysis_list,
            generation_id=generation_id,
            expected_active=expected_active,
            repository_scope=repository_scope,
            deadline=deadline,
            cancelled=cancelled,
        )
        _check_stop(deadline, cancelled)
        fsync_file(database_path)
        fsync_directory(generation_path)
        search_artifact = _generation_search_artifact(
            snapshot, generation_path, deadline=deadline, cancelled=cancelled
        )
        vectors = _generation_vector_artifacts(
            snapshot,
            generation_path,
            deadline=deadline,
            cancelled=cancelled,
            reuse_from=_vector_reuse_source(catalog, parent_generation_id),
        )
        _kill_if(kill_point, "after_database_commit")
        manifest = _write_generation_manifests(
            generation_path,
            database_path,
            source_manifest=source_manifest,
            source_manifest_bytes=source_manifest_bytes,
            source_manifest_sha256=source_manifest_sha256,
            incremental_manifest=incremental_manifest,
            search_artifact=search_artifact,
            vectors=vectors,
            generation_id=generation_id,
            parent_generation_id=parent_generation_id,
            collector_version=collector_version,
            extractor_version=extractor_version,
            graph_extractor_version=graph_extractor_version,
            repository_scope=repository_scope,
            graph_schema=graph_schema,
            snapshot=snapshot,
            code_capture=code_capture,
            deadline=deadline,
            cancelled=cancelled,
        )
        # Perform semantic validation once and carry the resulting
        # process-local capability through registration and activation.
        candidate = catalog._validate_candidate(  # noqa: SLF001
            generation_id,
            expected_repository_scope=repository_scope,
            deadline=deadline,
            cancelled=cancelled,
        )
        _kill_if(kill_point, "after_validation")
        if not activate:
            catalog._register_validated(  # noqa: SLF001
                candidate, deadline=deadline, cancelled=cancelled
            )
            return BuildResult(
                generation_id=generation_id,
                generation_path=generation_path,
                manifest=manifest,
                activated=False,
            )
        activated = _activated_generation(
            catalog,
            candidate,
            state,
            generation_id=generation_id,
            snapshot=snapshot,
            publication_root=publication_root,
            expected_active=expected_active,
            repository_scope=repository_scope,
            coordinator=coordinator,
            kill_point=kill_point,
            deadline=deadline,
            cancelled=cancelled,
        )
        result = BuildResult(
            generation_id=generation_id,
            generation_path=generation_path,
            manifest=manifest,
            activated=activated,
        )
        _kill_if(kill_point, "after_activation")
        return result
    except KillPointError:
        # Kill-point aborts deliberately leave the partial state on disk
        # so tests can verify the catalog ignores orphans and the prior
        # generation stays readable.
        raise
    except BaseException:
        _discard_failed_generation(catalog, generation_id, state, deadline, cancelled)
        raise


def _kill_if(kill_point: str | None, name: str) -> None:
    """A configured kill point simulates a crash exactly here."""
    if kill_point == name:
        raise KillPointError(kill_point)


def _v3_only(value: object, graph_schema: evidence_graph.GraphSchema) -> object:
    if graph_schema is evidence_graph.GraphSchema.V3:
        return value
    return None


def _write_generation_database(
    database_path: Path,
    *,
    graph_schema: evidence_graph.GraphSchema,
    sources_list: list[Mapping[str, object]],
    source_bytes_snapshot: Mapping[str, bytes],
    records: Mapping[str, list[Mapping[str, object]]],
    verified_analysis_list: list[VerifiedAnalysisBatch],
    generation_id: str,
    expected_active: str | None,
    repository_scope: RepositoryScope | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    evidence_graph.create_generation_database(
        database_path,
        schema=graph_schema,
        sources=sources_list,
        source_bytes=source_bytes_snapshot,
        nodes=records["nodes"],
        occurrences=records["occurrences"],
        assertions=records["assertions"],
        evidence=records["evidence"],
        observations=records["observations"],
        dependencies=records["dependencies"],
        verified_analyses=verified_analysis_list,
        publication_generation_id=_v3_only(generation_id, graph_schema),
        publication_expected_active=_v3_only(expected_active, graph_schema),
        repository_scope=_v3_only(repository_scope, graph_schema),
        deadline=deadline,
        cancelled=cancelled,
    )


def _generation_search_artifact(
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    generation_path: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
):
    if snapshot is None:
        return None
    import search_memory

    return search_memory.build_generation_fts(
        snapshot, generation_path, deadline=deadline, cancelled=cancelled
    )


def _vector_reuse_source(
    catalog: generation_catalog.GenerationCatalog, parent_generation_id: str | None
) -> Path | None:
    """The parent generation, when there is one on disk to read vectors from."""
    if not parent_generation_id:
        return None
    candidate = catalog.generations_path / parent_generation_id
    if not candidate.is_dir():
        return None
    return candidate


def _generation_vector_artifacts(
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    generation_path: Path,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    reuse_from: Path | None = None,
):
    """Vectors are what let a question reach a page written in another language.

    Re-encoding every chunk of every build was 595.1 of one 721.2-second pass —
    82.5% — on a corpus where almost nothing had changed. The parent generation
    already stores one chunk digest per row beside its matrix, so it is the
    cache, and an unchanged chunk keeps the vector the same model gave it.
    """
    if snapshot is None:
        return None
    import search_memory

    return search_memory.build_generation_vectors_if_available(
        snapshot,
        generation_path,
        deadline=deadline,
        cancelled=cancelled,
        reuse_from=reuse_from,
    )


def _manifest_versions(snapshot: corpus_snapshot.CorpusSnapshot | None):
    """A complete generation carries the search tokenizer identity; a bare one does not."""
    if snapshot is None:
        return (
            CORPUS_GENERATION_SCHEMA_VERSION,
            DEFAULT_TOKENIZER_VERSION,
            DEFAULT_TOKENIZER_CONFIG_SHA256,
        )
    import search_memory

    return (
        COMPLETE_CORPUS_GENERATION_SCHEMA_VERSION,
        search_memory.GENERATION_TOKENIZER_VERSION,
        search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
    )


def _write_generation_manifests(
    generation_path: Path,
    database_path: Path,
    *,
    source_manifest: Mapping[str, object],
    source_manifest_bytes: bytes,
    source_manifest_sha256: str,
    incremental_manifest: Mapping[str, object] | None,
    search_artifact: Mapping[str, object] | None,
    vectors: Mapping[str, object] | None,
    generation_id: str,
    parent_generation_id: str | None,
    collector_version: str,
    extractor_version: str,
    graph_extractor_version: str,
    repository_scope: RepositoryScope | None,
    graph_schema: evidence_graph.GraphSchema,
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    code_capture: corpus_snapshot.CodeCaptureContract | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> Mapping[str, object]:
    """Both files are canonical JSON, fsynced, and the directory is fsynced."""
    _write_canonical_file(
        generation_path / "source-manifest.json",
        source_manifest,
        deadline=deadline,
        cancelled=cancelled,
    )
    incremental_manifest_bytes = None
    if incremental_manifest is not None:
        incremental_manifest_bytes = canonical_json_bytes(incremental_manifest)
        _write_canonical_file(
            generation_path / "incremental-manifest.json",
            incremental_manifest,
            deadline=deadline,
            cancelled=cancelled,
        )
    database_size, database_sha256 = _hash_file(
        database_path, deadline=deadline, cancelled=cancelled
    )
    schema_version, tokenizer_version, tokenizer_config_sha256 = _manifest_versions(
        snapshot
    )
    manifest = _build_manifest(
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        collector_version=collector_version,
        extractor_version=extractor_version,
        graph_extractor_version=graph_extractor_version,
        source_manifest_sha256=source_manifest_sha256,
        database_size=database_size,
        database_sha256=database_sha256,
        source_manifest_bytes=source_manifest_bytes,
        repository_scope=repository_scope,
        graph_schema=graph_schema,
        schema_version=schema_version,
        tokenizer_version=tokenizer_version,
        tokenizer_config_sha256=tokenizer_config_sha256,
        search_artifact=search_artifact,
        incremental_manifest_bytes=incremental_manifest_bytes,
        code_capture=code_capture,
        vectors=vectors,
    )
    _write_canonical_file(
        generation_path / "manifest.json", manifest, deadline=deadline, cancelled=cancelled
    )
    fsync_directory(generation_path)
    return manifest


def _activated_catalog_generation(
    catalog: generation_catalog.GenerationCatalog,
    candidate: object,
    *,
    generation_id: str,
    expected_active: str | None,
    kill_point: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    catalog._register_validated(  # noqa: SLF001
        candidate, deadline=deadline, cancelled=cancelled
    )
    _kill_if(kill_point, "before_activation")
    activated = catalog._activate_validated(  # noqa: SLF001
        candidate,
        expected_active=expected_active,
        deadline=deadline,
        cancelled=cancelled,
    )
    if not activated:
        catalog.discard_unactivated(generation_id, deadline=deadline, cancelled=cancelled)
    return activated


def _activated_generation(
    catalog: generation_catalog.GenerationCatalog,
    candidate: object,
    state: dict[str, bool],
    *,
    generation_id: str,
    snapshot: corpus_snapshot.CorpusSnapshot | None,
    publication_root: Path | None,
    expected_active: str | None,
    repository_scope: RepositoryScope | None,
    coordinator: object | None,
    kill_point: str | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """Register the new generation, then advance the pointer under CAS."""
    if snapshot is None:
        return _activated_catalog_generation(
            catalog,
            candidate,
            generation_id=generation_id,
            expected_active=expected_active,
            kill_point=kill_point,
            deadline=deadline,
            cancelled=cancelled,
        )
    import search_memory

    _kill_if(kill_point, "before_activation")
    state["publication_attempted"] = True
    return search_memory._publish_validated_generation(  # noqa: SLF001
        snapshot,
        Path(publication_root),
        catalog,
        generation_id,
        candidate,
        expected_repository_scope=repository_scope,
        expected_active=expected_active,
        coordinator=coordinator,
        deadline=deadline,
        cancelled=cancelled,
    )


def _discard_failed_generation(
    catalog: generation_catalog.GenerationCatalog,
    generation_id: str,
    state: Mapping[str, bool],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    cleanup_options = {}
    if state["publication_attempted"]:
        cleanup_options = {"deadline": deadline, "cancelled": cancelled}
    try:
        catalog.discard_unactivated(generation_id, **cleanup_options)
    except BaseException:
        # Preserve the publication failure. Catalog cleanup is fail-safe:
        # inability to prove the generation unreferenced leaves it on disk.
        pass


def _require_invalidation_fingerprints(fingerprints: object) -> None:
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != _INVALIDATION_KEYS:
        raise ValueError(
            "invalidation_fingerprints must contain exports, imports, signatures, aliases, "
            "and project_metadata"
        )
    if any(not _is_digest(item) for item in fingerprints.values()):
        raise ValueError("invalidation fingerprints must be lowercase SHA-256 digests")


def _is_sorted_unique_ids(dependencies: object) -> bool:
    if not isinstance(dependencies, tuple):
        return False
    if any(not isinstance(item, str) or not item for item in dependencies):
        return False
    return tuple(sorted(set(dependencies))) == dependencies


def _is_record_tuple(records: object) -> bool:
    if not isinstance(records, tuple):
        return False
    return all(isinstance(record, Mapping) for record in records)


def _require_record_collections(value: SourceExtraction) -> None:
    for collection in _RECORD_COLLECTIONS:
        if not _is_record_tuple(getattr(value, collection)):
            raise TypeError(f"{collection} must be a tuple of record mappings")


def _validated_extraction(value: object) -> SourceExtraction:
    if not isinstance(value, SourceExtraction):
        raise TypeError("extractor must return SourceExtraction")
    _require_invalidation_fingerprints(value.invalidation_fingerprints)
    _require_extraction_shape(value)
    _require_record_collections(value)
    return value


def _require_extraction_shape(value: SourceExtraction) -> None:
    if not _is_sorted_unique_ids(value.source_dependencies):
        raise ValueError("source_dependencies must be a sorted unique tuple of source IDs")
    if not isinstance(value.workspace_sensitive, bool):
        raise TypeError("workspace_sensitive must be a boolean")


def _declared_manifest_bytes(generation_manifest: Mapping[str, object]) -> int:
    """How many bytes to read: the number the sealed generation manifest names.

    `generation_catalog._validate_generation` has already hashed every artifact
    against `manifest.json` and refused a wrong size or digest, so by the time
    this is asked the declared size is verified fact, not a hint. Reading
    exactly that is a bound the corpus cannot outgrow — which a constant is not,
    and that is the whole defect: the old 64 MiB constant was passed by a
    158,075,010-byte manifest and the manifest was thrown away instead.
    """
    for artifact in generation_manifest.get("artifacts", ()):
        if artifact.get("path") == "incremental-manifest.json":
            return int(artifact["size"])
    raise ValueError("the sealed manifest does not declare its incremental manifest")


def _load_incremental_manifest(
    catalog: generation_catalog.GenerationCatalog,
    generation_id: str,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[Mapping[str, object] | None, Mapping[str, object] | None]:
    _check_stop(deadline, cancelled)
    generation_path = catalog.generations_path / generation_id
    if not (generation_path / "incremental-manifest.json").exists():
        return None, None
    generation_manifest, _seal = generation_catalog._validate_generation(  # noqa: SLF001 - reuse must validate the sealed parent
        generation_path,
        catalog.state_root,
        deadline=deadline,
        cancelled=cancelled,
    )
    raw = read_runtime_bytes(
        generation_path / "incremental-manifest.json",
        catalog.state_root,
        max_bytes=_declared_manifest_bytes(generation_manifest),
    )
    _check_stop(deadline, cancelled)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("incremental manifest must contain valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
        raise ValueError("incremental manifest must be a canonical JSON object")
    return _validated_incremental_manifest(value), generation_manifest


_LANGUAGE_MANIFEST_VERSIONS = {
    INCREMENTAL_MANIFEST_VERSION,
    "evidence-graph-incremental/v2",
    "evidence-graph-incremental/v3",
    "evidence-graph-incremental/v4",
}

_BASE_ENTRY_KEYS = {
    "source_id",
    "relative_path",
    "sha256",
    "source_dependencies",
    "invalidation_fingerprints",
    "records",
}


def _entry_keys_for(version: str) -> set[str]:
    keys = set(_BASE_ENTRY_KEYS)
    if version in _LANGUAGE_MANIFEST_VERSIONS:
        keys.add("language")
    if version in _WORKSPACE_SENSITIVE_VERSIONS:
        keys.add("workspace_sensitive")
    elif version == "evidence-graph-incremental/v3":
        keys.add("workspace_sensitive_sources")
    return keys


def _is_bounded_string_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) and item for item in value)


def _require_sorted_unique_list(value: object, message: str) -> None:
    if not _is_bounded_string_list(value):
        raise ValueError(message)
    if value != sorted(set(value)):
        raise ValueError(message)


def _require_manifest_version(version: object) -> None:
    if version not in {
        INCREMENTAL_MANIFEST_VERSION,
        *_LEGACY_INCREMENTAL_MANIFEST_VERSIONS,
    }:
        raise ValueError("incremental manifest has an unsupported version")


def _require_reuse_config(config: object) -> None:
    if not isinstance(config, Mapping) or set(config) != set(
        IncrementalReuseConfig.__dataclass_fields__
    ):
        raise ValueError("incremental reuse config must be a closed object")
    IncrementalReuseConfig(**config)


def _require_closed_entry(entry: object, version: str) -> None:
    if not isinstance(entry, Mapping) or set(entry) != _entry_keys_for(version):
        raise ValueError("incremental source entries must be closed objects")


def _require_unique_source_id(entry: Mapping[str, object], seen_sources: set[str]) -> None:
    source_id = entry["source_id"]
    if not isinstance(source_id, str) or not source_id or source_id in seen_sources:
        raise ValueError("incremental source IDs must be unique non-empty strings")
    seen_sources.add(source_id)


def _require_entry_path(entry: Mapping[str, object]) -> None:
    if not isinstance(entry["relative_path"], str) or not entry["relative_path"]:
        raise ValueError("incremental source paths must be non-empty strings")


def _require_entry_language(entry: Mapping[str, object], version: str) -> None:
    if version not in _LANGUAGE_MANIFEST_VERSIONS:
        return
    if entry["language"] is None or isinstance(entry["language"], str):
        return
    raise ValueError("incremental source language must be a string or null")


def _require_legacy_sensitive_sources(sensitive_sources: object) -> None:
    message = "incremental workspace-sensitive sources must be bounded, sorted, and unique"
    _require_sorted_unique_list(sensitive_sources, message)
    if len(sensitive_sources) > MAX_LEGACY_WORKSPACE_SENSITIVE_SOURCES:
        raise ValueError(message)


def _require_entry_workspace(entry: Mapping[str, object], version: str) -> None:
    if version in _WORKSPACE_SENSITIVE_VERSIONS:
        _require_workspace_flag(entry)
        return
    if version != "evidence-graph-incremental/v3":
        return
    _require_legacy_sensitive_sources(entry["workspace_sensitive_sources"])


def _require_workspace_flag(entry: Mapping[str, object]) -> None:
    if not isinstance(entry["workspace_sensitive"], bool):
        raise TypeError("incremental workspace_sensitive must be a boolean")


def _require_entry_fingerprints(fingerprints: object) -> None:
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != _INVALIDATION_KEYS:
        raise ValueError("incremental invalidation fingerprints are malformed")
    if any(not _is_digest(item) for item in fingerprints.values()):
        raise ValueError("incremental invalidation fingerprints are malformed")


def _require_entry_records(records: object) -> None:
    if not isinstance(records, Mapping) or set(records) != set(_RECORD_COLLECTIONS):
        raise ValueError("incremental source record membership must be a closed object")
    for record_ids in records.values():
        _require_sorted_unique_list(
            record_ids, "incremental record IDs must be sorted and unique"
        )


def _require_source_entry(
    entry: object, version: str, seen_sources: set[str]
) -> None:
    _require_closed_entry(entry, version)
    _require_unique_source_id(entry, seen_sources)
    _require_entry_path(entry)
    _require_entry_language(entry, version)
    if not _is_digest(entry["sha256"]):
        raise ValueError("incremental source hashes must be lowercase SHA-256 digests")
    _require_sorted_unique_list(
        entry["source_dependencies"],
        "incremental source dependencies must be sorted and unique",
    )
    _require_entry_workspace(entry, version)
    _require_entry_fingerprints(entry["invalidation_fingerprints"])
    _require_entry_records(entry["records"])


_REQUIRED_MANIFEST_KEYS = frozenset(
    {"version", "reuse_config", "sources", "record_dependencies"}
)
#: `record_dependencies` is a bounded sample from v5 on, so a manifest that
#: carries one may also state how many rows there really are. Optional rather
#: than required so that a manifest written before the bound existed, or built
#: by hand, still validates.
_OPTIONAL_MANIFEST_KEYS = frozenset({"record_dependencies_total"})


def _require_manifest_keys(value: Mapping[str, object]) -> None:
    keys = set(value)
    if not _REQUIRED_MANIFEST_KEYS <= keys:
        raise ValueError("incremental manifest must be a closed object")
    if not keys <= _REQUIRED_MANIFEST_KEYS | _OPTIONAL_MANIFEST_KEYS:
        raise ValueError("incremental manifest must be a closed object")


def _require_manifest_sources(sources: object, version: str) -> None:
    if not isinstance(sources, list):
        raise TypeError("incremental manifest sources must be an array")
    seen_sources: set[str] = set()
    for entry in sources:
        _require_source_entry(entry, version, seen_sources)


def _require_manifest_dependencies(value: Mapping[str, object]) -> None:
    if not isinstance(value["record_dependencies"], list):
        raise TypeError("incremental record dependencies must be an array")
    if "record_dependencies_total" not in value:
        return
    _require_dependency_total(value)


def _require_dependency_total(value: Mapping[str, object]) -> None:
    total = value["record_dependencies_total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("incremental record dependency total must be a whole number")
    if total < len(value["record_dependencies"]):
        raise ValueError("incremental record dependency total is smaller than its sample")


def _validated_incremental_manifest(value: Mapping[str, object]) -> Mapping[str, object]:
    version = value.get("version")
    _require_manifest_version(version)
    _require_manifest_keys(value)
    _require_reuse_config(value["reuse_config"])
    _require_manifest_sources(value["sources"], version)
    _require_manifest_dependencies(value)
    return value


def _workspace_sensitive_source_ids(
    entries: Mapping[str, Mapping[str, object]],
) -> set[str]:
    """Derive the workspace-sensitive owners in one pass over source entries."""
    return {
        source_id
        for source_id, entry in entries.items()
        if entry["workspace_sensitive"] is True
    }


def _node_row_record(row: sqlite3.Row) -> dict[str, object]:
    return {
        "node_id": row["node_id"],
        "kind": row["kind"],
        "identity_scheme": row["identity_scheme"],
        "identity_key": row["identity_key"],
        "metadata": json.loads(row["metadata_json"]),
    }


def _decoded_literal(value: object) -> object:
    if value is None:
        return None
    return json.loads(value)


def _assertion_row_record(row: sqlite3.Row) -> dict[str, object]:
    record = dict(row)
    record["literal"] = _decoded_literal(record.pop("literal_json"))
    return record


def _plain_row_record(row: sqlite3.Row) -> dict[str, object]:
    return dict(row)


_ROW_RECORD_BUILDERS = {
    "nodes": _node_row_record,
    "occurrences": _plain_row_record,
    "assertions": _assertion_row_record,
    "evidence": _plain_row_record,
    "observations": _plain_row_record,
    "dependencies": _plain_row_record,
}


def _row_record(collection: str, row: sqlite3.Row) -> dict[str, object]:
    builder = _ROW_RECORD_BUILDERS.get(collection)
    if builder is None:
        raise AssertionError(f"unknown record collection: {collection}")
    return builder(row)


def _parent_records(
    database_path: Path,
    source_entries: Mapping[str, Mapping[str, object]],
    reused_sources: set[str],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, Mapping[str, object]]]:
    wanted = _wanted_record_ids(source_entries, reused_sources)
    loaded: dict[str, dict[str, Mapping[str, object]]] = {
        collection: {} for collection in _RECORD_COLLECTIONS
    }
    uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
        database.row_factory = sqlite3.Row
        database.set_progress_handler(
            _progress_guard(deadline, cancelled), evidence_graph.PROGRESS_OPCODES
        )
        _read_parent_records(database, wanted, loaded, deadline, cancelled)
    _require_complete_parent_records(loaded, wanted)
    return loaded


def _wanted_record_ids(
    source_entries: Mapping[str, Mapping[str, object]], reused_sources: set[str]
) -> dict[str, set[str]]:
    return {
        collection: {
            str(record_id)
            for source_id in reused_sources
            for record_id in source_entries[source_id]["records"][collection]
        }
        for collection in _RECORD_COLLECTIONS
    }


def _stop_requested(
    deadline: float | None, cancelled: Callable[[], bool] | None
) -> bool:
    if bool(cancelled and cancelled()):
        return True
    return deadline is not None and time.monotonic() >= deadline


def _progress_guard(deadline: float | None, cancelled: Callable[[], bool] | None):
    def guard() -> int:
        return int(_stop_requested(deadline, cancelled))

    return guard


def _collection_table(collection: str) -> str:
    if collection == "dependencies":
        return "dependency"
    return collection.removesuffix("s")


def _load_collection_records(
    database: sqlite3.Connection,
    collection: str,
    wanted: set[str],
    loaded: dict[str, Mapping[str, object]],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    key = _RECORD_KEYS[collection]
    table = _collection_table(collection)
    for row in database.execute(f"SELECT * FROM {table} ORDER BY {key}"):
        _check_stop(deadline, cancelled)
        record_id = str(row[key])
        if record_id in wanted:
            loaded[record_id] = _row_record(collection, row)


def _read_parent_records(
    database: sqlite3.Connection,
    wanted: Mapping[str, set[str]],
    loaded: Mapping[str, dict[str, Mapping[str, object]]],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> None:
    try:
        for collection in _RECORD_COLLECTIONS:
            _load_collection_records(
                database,
                collection,
                wanted[collection],
                loaded[collection],
                deadline,
                cancelled,
            )
    except sqlite3.OperationalError as exc:
        if _stop_requested(deadline, cancelled):
            raise TimeoutError(
                "incremental parent read cancelled or deadline reached"
            ) from exc
        raise
    finally:
        database.set_progress_handler(None, 0)


def _require_complete_parent_records(
    loaded: Mapping[str, dict[str, Mapping[str, object]]],
    wanted: Mapping[str, set[str]],
) -> None:
    for collection in _RECORD_COLLECTIONS:
        if set(loaded[collection]) != wanted[collection]:
            raise ValueError("incremental manifest references missing parent records")


def _renames(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
    added: set[str],
    deleted: set[str],
) -> tuple[tuple[str, str], ...]:
    old_by_hash = _grouped_by_hash(previous, deleted)
    new_by_hash = _grouped_by_hash(current, added)
    pairs = [
        (source_id, source_id)
        for source_id in sorted(previous.keys() & current.keys())
        if _same_content_moved(previous, current, source_id)
    ]
    for digest in sorted(old_by_hash.keys() & new_by_hash.keys()):
        old = sorted(old_by_hash[digest])
        new = sorted(new_by_hash[digest])
        pairs.extend(zip(old, new, strict=False))
    return tuple(sorted(pairs))


def _grouped_by_hash(
    entries: Mapping[str, Mapping[str, object]], source_ids: Iterable[str]
) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for source_id in source_ids:
        grouped.setdefault(str(entries[source_id]["sha256"]), []).append(source_id)
    return grouped


def _same_content_moved(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
    source_id: str,
) -> bool:
    """The same bytes under a new path: a rename, not an edit."""
    if previous[source_id].get("sha256") != current[source_id].get("sha256"):
        return False
    return previous[source_id].get("relative_path") != current[source_id].get(
        "relative_path"
    )


def _record_ids_by_owner(
    ownership: Mapping[tuple[str, str], set[str]],
    source_ids: Iterable[str],
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Invert record ownership once, preserving sorted manifest record IDs."""
    grouped = {
        source_id: {collection: [] for collection in _RECORD_COLLECTIONS}
        for source_id in source_ids
    }
    for (collection, record_id), owners in ownership.items():
        _check_stop(deadline, cancelled)
        _add_record_owners(grouped, collection, record_id, owners)
    for records in grouped.values():
        _check_stop(deadline, cancelled)
        _sort_record_ids(records)
    return grouped


def _add_record_owners(
    grouped: Mapping[str, dict[str, list[str]]],
    collection: str,
    record_id: str,
    owners: Iterable[str],
) -> None:
    for source_id in owners:
        grouped[source_id][collection].append(record_id)


def _sort_record_ids(records: Mapping[str, list[str]]) -> None:
    for record_ids in records.values():
        record_ids.sort()


def build_incremental_generation(
    catalog: generation_catalog.GenerationCatalog,
    *,
    sources: Iterable[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    extractor: Callable[..., SourceExtraction],
    reuse_config: IncrementalReuseConfig,
    generation_id: str,
    parent_generation_id: str | None = None,
    policy: Mapping[str, object] | None = None,
    collector_version: str = corpus_snapshot.COLLECTOR_VERSION,
    expected_active: str | None = None,
    activate: bool = True,
    kill_point: str | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    repository_scope: RepositoryScope | None = None,
    snapshot: corpus_snapshot.CorpusSnapshot | None = None,
    publication_root: Path | None = None,
    coordinator: object | None = None,
) -> IncrementalBuildResult:
    """Build a complete immutable generation while reusing exact parent records."""
    repository_scope = _validated_incremental_inputs(
        catalog, reuse_config, extractor, repository_scope
    )
    repository_scope_object = (
        None if repository_scope is None else repository_scope.as_dict()
    )
    sources_list = [
        dict(source)
        for source in _materialize(
            sources,
            label="sources",
            limit=evidence_graph.MAX_VALIDATION_ROWS,
            deadline=deadline,
            cancelled=cancelled,
        )
    ]
    source_snapshot = _verify_source_snapshot(
        sources_list, source_bytes, deadline=deadline, cancelled=cancelled
    )
    current = _current_sources(sources_list)
    parent_manifest, parent_entries, config_matches = _incremental_parent_state(
        catalog,
        parent_generation_id,
        reuse_config,
        repository_scope_object,
        deadline=deadline,
        cancelled=cancelled,
    )
    delta = _incremental_delta(
        current, parent_entries, parent_manifest, reuse_config, config_matches
    )
    runner = _IncrementalExtractor(
        extractor,
        sources_list,
        source_snapshot,
        set(current),
        deadline=deadline,
        cancelled=cancelled,
    )
    rebuild = delta["rebuild"]
    runner.run_all(rebuild)
    _expanded_rebuild(runner, delta, config_matches=config_matches)
    current_ids = set(current)
    reused = current_ids - rebuild
    merged, ownership = _merged_records(
        _reused_parent_records(
            catalog,
            parent_generation_id,
            parent_entries,
            reused,
            deadline=deadline,
            cancelled=cancelled,
        ),
        parent_entries,
        reused,
        rebuild,
        runner.extracted,
    )
    records_by_owner = _record_ids_by_owner(
        ownership,
        sorted(current_ids),
        deadline=deadline,
        cancelled=cancelled,
    )
    source_entries = _manifest_source_entries(
        current_ids, current, runner.extracted, parent_entries, records_by_owner
    )
    entry_by_id = {str(entry["source_id"]): entry for entry in source_entries}
    dependency_rows, dependency_total = _record_dependency_rows(
        ownership, entry_by_id, rebuild
    )
    incremental_manifest = _stored_incremental_manifest(
        reuse_config,
        source_entries,
        dependency_rows,
        dependency_total,
    )
    _check_stop(deadline, cancelled)
    built = build_full_generation(
        catalog,
        sources=sources_list,
        source_bytes=source_snapshot,
        nodes=merged["nodes"].values(),
        occurrences=merged["occurrences"].values(),
        assertions=merged["assertions"].values(),
        evidence=merged["evidence"].values(),
        observations=merged["observations"].values(),
        dependencies=merged["dependencies"].values(),
        generation_id=generation_id,
        parent_generation_id=parent_generation_id,
        policy=policy,
        collector_version=collector_version,
        extractor_version=_incremental_extractor_version(snapshot, reuse_config),
        graph_extractor_version=reuse_config.extractor_version,
        expected_active=expected_active,
        activate=activate,
        kill_point=kill_point,
        deadline=deadline,
        cancelled=cancelled,
        incremental_manifest=incremental_manifest,
        repository_scope=repository_scope,
        snapshot=snapshot,
        publication_root=publication_root,
        coordinator=coordinator,
    )
    return IncrementalBuildResult(
        generation_id=built.generation_id,
        generation_path=built.generation_path,
        manifest=built.manifest,
        activated=built.activated,
        added_sources=tuple(sorted(delta["added"])),
        changed_sources=tuple(sorted(delta["changed"])),
        deleted_sources=tuple(sorted(delta["deleted"])),
        renamed_sources=delta["renamed"],
        reused_sources=tuple(sorted(reused)),
        rebuilt_sources=tuple(sorted(rebuild)),
    )


def _validated_incremental_inputs(
    catalog: object,
    reuse_config: object,
    extractor: object,
    repository_scope: RepositoryScope | None,
) -> RepositoryScope | None:
    _require_type(
        catalog,
        generation_catalog.GenerationCatalog,
        "catalog must be a GenerationCatalog",
    )
    _require_type(
        reuse_config, IncrementalReuseConfig, "reuse_config must be IncrementalReuseConfig"
    )
    if not callable(extractor):
        raise TypeError("extractor must be callable")
    _require_optional_type(
        repository_scope,
        RepositoryScope,
        "repository_scope must be a RepositoryScope or None",
    )
    if repository_scope is None:
        return None
    return RepositoryScope.from_dict(repository_scope.as_dict())


def _current_sources(
    sources_list: list[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    current = {str(source["source_id"]): source for source in sources_list}
    if len(current) != len(sources_list):
        raise ValueError("captured sources must have unique source IDs")
    return current


def _comparable_reuse_config(config: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in config.items() if key != "workspace_manifest_sha256"
    }


def _reuse_config_matches(
    parent_manifest: Mapping[str, object],
    parent_generation_manifest: Mapping[str, object] | None,
    reuse_config: IncrementalReuseConfig,
    repository_scope_object: Mapping[str, object] | None,
) -> bool:
    """Records may be reused only when the parent was built the same way.

    Identity, not equality. A `RepositoryScope` record carries `git_commit`,
    and this vault commits its own runtime, so comparing the whole record meant
    that one commit was enough to reuse nothing -- the fourth site of the same
    mistake, after NEW-65, NEW-90 and NEW-111. The commit stays in the manifest
    as provenance; what the parent was *built the same way* from is decided by
    `reuse_config` above and by each source's own digest below. See NEW-138.
    """
    if not _parent_reuse_config_matches(parent_manifest, reuse_config):
        return False
    if parent_generation_manifest is None:
        return False
    return same_repository_record(
        parent_generation_manifest.get("repository_scope"), repository_scope_object
    )


def _parent_reuse_config_matches(
    parent_manifest: Mapping[str, object], reuse_config: IncrementalReuseConfig
) -> bool:
    if parent_manifest.get("version") != INCREMENTAL_MANIFEST_VERSION:
        return False
    parent_config = parent_manifest.get("reuse_config")
    if not isinstance(parent_config, Mapping):
        return False
    return _comparable_reuse_config(parent_config) == _comparable_reuse_config(
        asdict(reuse_config)
    )


def _incremental_parent_state(
    catalog: generation_catalog.GenerationCatalog,
    parent_generation_id: str | None,
    reuse_config: IncrementalReuseConfig,
    repository_scope_object: Mapping[str, object] | None,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
):
    if parent_generation_id is None:
        return None, {}, False
    parent_manifest, parent_generation_manifest = _load_incremental_manifest(
        catalog, parent_generation_id, deadline=deadline, cancelled=cancelled
    )
    if parent_manifest is None:
        return None, {}, False
    parent_entries = {
        str(entry["source_id"]): entry for entry in parent_manifest.get("sources", [])
    }
    config_matches = _reuse_config_matches(
        parent_manifest, parent_generation_manifest, reuse_config, repository_scope_object
    )
    return parent_manifest, parent_entries, config_matches


def _source_differs(
    current_source: Mapping[str, object], parent_source: Mapping[str, object]
) -> bool:
    if current_source["sha256"] != parent_source.get("sha256"):
        return True
    if current_source["relative_path"] != parent_source.get("relative_path"):
        return True
    return current_source.get("language") != parent_source.get("language")


def _workspace_surface_moved(
    current_source: Mapping[str, object], parent_source: Mapping[str, object]
) -> bool:
    if current_source["relative_path"] != parent_source.get("relative_path"):
        return True
    return current_source.get("language") != parent_source.get("language")


def _workspace_source_ids(sources: Mapping[str, Mapping[str, object]]) -> set[str]:
    return {
        source_id
        for source_id, source in sources.items()
        if not str(source["relative_path"]).startswith("knowledge/")
    }


def _workspace_membership_changed(
    parent_manifest: Mapping[str, object],
    reuse_config: IncrementalReuseConfig,
    current: Mapping[str, Mapping[str, object]],
    parent_sources: Mapping[str, Mapping[str, object]],
    changed: set[str],
    added: set[str],
    deleted: set[str],
) -> bool:
    """Whether the workspace surface itself moved, not only its file contents."""
    parent_workspace_manifest = str(
        parent_manifest["reuse_config"]["workspace_manifest_sha256"]
    )
    if parent_workspace_manifest != reuse_config.workspace_manifest_sha256:
        return True
    current_workspace_ids = _workspace_source_ids(current)
    previous_workspace_ids = _workspace_source_ids(parent_sources)
    if added & current_workspace_ids or deleted & previous_workspace_ids:
        return True
    workspace_ids = current_workspace_ids | previous_workspace_ids
    return any(
        _workspace_entry_moved(source_id, workspace_ids, current, parent_sources)
        for source_id in changed
    )


def _workspace_entry_moved(
    source_id: str,
    workspace_ids: set[str],
    current: Mapping[str, Mapping[str, object]],
    parent_sources: Mapping[str, Mapping[str, object]],
) -> bool:
    if source_id not in workspace_ids:
        return False
    return _workspace_surface_moved(current[source_id], parent_sources[source_id])


def _incremental_delta(
    current: Mapping[str, Mapping[str, object]],
    parent_entries: Mapping[str, Mapping[str, object]],
    parent_manifest: Mapping[str, object] | None,
    reuse_config: IncrementalReuseConfig,
    config_matches: bool,
) -> dict[str, object]:
    """What changed since the parent, and therefore what has to be rebuilt."""
    current_ids = set(current)
    previous_ids = set(parent_entries)
    added = current_ids - previous_ids
    deleted = previous_ids - current_ids
    changed = {
        source_id
        for source_id in current_ids & previous_ids
        if _source_differs(current[source_id], parent_entries[source_id])
    }
    membership_changed = config_matches and _workspace_membership_changed(
        parent_manifest, reuse_config, current, parent_entries, changed, added, deleted
    )
    rebuild = _initial_rebuild(config_matches, current_ids, added, changed)
    if membership_changed:
        rebuild.update(_workspace_source_ids(current))
    return {
        "added": added,
        "deleted": deleted,
        "changed": changed,
        "renamed": _renames(parent_entries, current, added, deleted),
        "rebuild": rebuild,
        "membership_changed": membership_changed,
        "parent_entries": parent_entries,
        "current_ids": current_ids,
    }


def _initial_rebuild(
    config_matches: bool, current_ids: set[str], added: set[str], changed: set[str]
) -> set[str]:
    if not config_matches:
        return set(current_ids)
    return set(added | changed)


class _IncrementalExtractor:
    """Runs the extractor once per source and remembers what it produced."""

    def __init__(
        self,
        extractor: Callable[..., SourceExtraction],
        sources_list: list[Mapping[str, object]],
        source_snapshot: Mapping[str, bytes],
        current_ids: set[str],
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self._extractor = extractor
        self._sources = tuple(MappingProxyType(source) for source in sources_list)
        self._source_by_id = {
            str(source["source_id"]): source for source in self._sources
        }
        self._source_snapshot = source_snapshot
        self._source_bytes = MappingProxyType(source_snapshot)
        self._current_ids = current_ids
        self._deadline = deadline
        self._cancelled = cancelled
        self.extracted: dict[str, SourceExtraction] = {}

    def run(self, source_id: str) -> SourceExtraction:
        _check_stop(self._deadline, self._cancelled)
        result = _validated_extraction(
            self._extractor(
                self._source_by_id[source_id],
                self._source_snapshot[source_id],
                sources=self._sources,
                source_bytes=self._source_bytes,
                deadline=self._deadline,
                cancelled=self._cancelled,
            )
        )
        unknown = set(result.source_dependencies) - self._current_ids
        if unknown:
            raise ValueError(
                f"source extraction has unknown dependencies: {sorted(unknown)!r}"
            )
        self.extracted[source_id] = result
        return result

    def run_all(self, source_ids: Iterable[str]) -> None:
        for source_id in sorted(source_ids):
            self.run(source_id)


def _semantic_changes(
    extracted: Mapping[str, SourceExtraction],
    changed: set[str],
    parent_entries: Mapping[str, Mapping[str, object]],
) -> set[str]:
    return {
        source_id
        for source_id in changed
        if dict(extracted[source_id].invalidation_fingerprints or {})
        != parent_entries[source_id].get("invalidation_fingerprints")
    }


def _workspace_invalidated(
    semantic_changes: set[str],
    membership_changed: bool,
    parent_entries: Mapping[str, Mapping[str, object]],
    current_ids: set[str],
    rebuild: set[str],
) -> set[str]:
    if not semantic_changes or membership_changed:
        return set()
    return (_workspace_sensitive_source_ids(parent_entries) & current_ids) - rebuild


def _newly_invalidated(
    current_ids: set[str],
    rebuild: set[str],
    parent_entries: Mapping[str, Mapping[str, object]],
    invalidated: set[str],
) -> set[str]:
    return {
        source_id
        for source_id in current_ids - rebuild
        if set(parent_entries[source_id].get("source_dependencies", ())) & invalidated
    }


def _expanded_rebuild(
    runner: _IncrementalExtractor, delta: Mapping[str, object], *, config_matches: bool
) -> None:
    """Follow invalidation through dependencies until nothing new falls out."""
    if not config_matches:
        return
    parent_entries = delta["parent_entries"]
    current_ids = delta["current_ids"]
    rebuild = delta["rebuild"]
    semantic_changes = _semantic_changes(
        runner.extracted, delta["changed"], parent_entries
    )
    workspace_invalidated = _workspace_invalidated(
        semantic_changes,
        bool(delta["membership_changed"]),
        parent_entries,
        current_ids,
        rebuild,
    )
    runner.run_all(workspace_invalidated)
    rebuild.update(workspace_invalidated)
    invalidated = set(semantic_changes | delta["deleted"] | workspace_invalidated)
    while invalidated:
        newly = _newly_invalidated(current_ids, rebuild, parent_entries, invalidated)
        if not newly:
            break
        runner.run_all(newly)
        rebuild.update(newly)
        invalidated = newly


def _reused_parent_records(
    catalog: generation_catalog.GenerationCatalog,
    parent_generation_id: str | None,
    parent_entries: Mapping[str, Mapping[str, object]],
    reused: set[str],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, Mapping[str, object]]]:
    if not reused or parent_generation_id is None:
        return {collection: {} for collection in _RECORD_COLLECTIONS}
    return _parent_records(
        catalog.generations_path / parent_generation_id / "evidence.sqlite3",
        parent_entries,
        reused,
        deadline=deadline,
        cancelled=cancelled,
    )


def _require_consistent_record(
    existing: Mapping[str, object] | None,
    candidate: Mapping[str, object],
    collection: str,
    record_id: str,
) -> None:
    if existing is not None and existing != candidate:
        raise ValueError(f"conflicting {collection} record {record_id!r}")


def _merge_collection_records(
    target: dict[str, Mapping[str, object]],
    ownership: dict[tuple[str, str], set[str]],
    collection: str,
    records: Iterable[Mapping[str, object]],
    source_id: str,
) -> None:
    key = _RECORD_KEYS[collection]
    for record in records:
        record_id = str(record[key])
        candidate = dict(record)
        _require_consistent_record(
            target.get(record_id), candidate, collection, record_id
        )
        target[record_id] = candidate
        ownership.setdefault((collection, record_id), set()).add(source_id)


def _claim_reused_records(
    ownership: dict[tuple[str, str], set[str]],
    records: Mapping[str, Iterable[str]],
    source_id: str,
) -> None:
    for collection in _RECORD_COLLECTIONS:
        for record_id in records[collection]:
            ownership.setdefault((collection, str(record_id)), set()).add(source_id)


def _merged_records(
    parent_records: Mapping[str, dict[str, Mapping[str, object]]],
    parent_entries: Mapping[str, Mapping[str, object]],
    reused: set[str],
    rebuild: set[str],
    extracted: Mapping[str, SourceExtraction],
):
    merged: dict[str, dict[str, Mapping[str, object]]] = {
        collection: dict(parent_records[collection])
        for collection in _RECORD_COLLECTIONS
    }
    ownership: dict[tuple[str, str], set[str]] = {}
    for source_id in reused:
        _claim_reused_records(ownership, parent_entries[source_id]["records"], source_id)
    for source_id in sorted(rebuild):
        result = extracted[source_id]
        for collection in _RECORD_COLLECTIONS:
            _merge_collection_records(
                merged[collection],
                ownership,
                collection,
                getattr(result, collection),
                source_id,
            )
    return merged, ownership


_LANGUAGE_ENTRY_VERSIONS = {
    "evidence-graph-incremental/v2",
    "evidence-graph-incremental/v3",
    "evidence-graph-incremental/v4",
    "evidence-graph-incremental/v5",
}


def _entry_source_facts(
    source_id: str,
    extracted: Mapping[str, SourceExtraction],
    parent_entries: Mapping[str, Mapping[str, object]],
):
    result = extracted.get(source_id)
    if result is None:
        entry = parent_entries[source_id]
        return (
            list(entry["source_dependencies"]),
            dict(entry["invalidation_fingerprints"]),
            bool(entry["workspace_sensitive"]),
        )
    return (
        list(result.source_dependencies),
        dict(result.invalidation_fingerprints or {}),
        result.workspace_sensitive,
    )


def _entry_language_field(source: Mapping[str, object]) -> dict[str, object]:
    if INCREMENTAL_MANIFEST_VERSION not in _LANGUAGE_ENTRY_VERSIONS:
        return {}
    return {"language": source.get("language")}


def _entry_workspace_field(workspace_sensitive: bool) -> dict[str, object]:
    if INCREMENTAL_MANIFEST_VERSION in _WORKSPACE_SENSITIVE_VERSIONS:
        return {"workspace_sensitive": workspace_sensitive}
    if INCREMENTAL_MANIFEST_VERSION == "evidence-graph-incremental/v3":
        return {"workspace_sensitive_sources": []}
    return {}


def _manifest_source_entries(
    current_ids: set[str],
    current: Mapping[str, Mapping[str, object]],
    extracted: Mapping[str, SourceExtraction],
    parent_entries: Mapping[str, Mapping[str, object]],
    records_by_owner: Mapping[str, dict[str, list[str]]],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for source_id in sorted(current_ids):
        dependencies, fingerprints, workspace_sensitive = _entry_source_facts(
            source_id, extracted, parent_entries
        )
        source = current[source_id]
        entries.append(
            {
                "source_id": source_id,
                "relative_path": str(source["relative_path"]),
                "sha256": str(source["sha256"]),
                "source_dependencies": dependencies,
                "invalidation_fingerprints": fingerprints,
                "records": records_by_owner[source_id],
                **_entry_language_field(source),
                **_entry_workspace_field(workspace_sensitive),
            }
        )
    return entries


def _record_source_ids(
    owners: set[str], entry_by_id: Mapping[str, Mapping[str, object]]
) -> list[str]:
    dependencies = set(owners)
    for owner in owners:
        dependencies.update(map(str, entry_by_id[owner]["source_dependencies"]))
    return sorted(dependencies)


def _record_status(owners: set[str], rebuild: set[str]) -> str:
    if owners & rebuild:
        return "rebuilt"
    return "reused"


def _record_dependency_rows(
    ownership: Mapping[tuple[str, str], set[str]],
    entry_by_id: Mapping[str, Mapping[str, object]],
    rebuild: set[str],
) -> tuple[list[dict[str, object]], int]:
    """A bounded, deterministic prefix of the audit rows, and their true total.

    Materialising one row per record is what made the manifest unstorable —
    158,075,010 bytes for 349,306 records on this vault — and no reader in
    `scripts/` consumes these rows: their `source_ids` is the owners plus their
    `source_dependencies`, and their `status` is whether any owner was rebuilt,
    both of which stay derivable from `sources` and the membership sidecar.
    """
    ordered = sorted(ownership.items())
    rows = [
        {
            "collection": collection,
            "record_id": record_id,
            "source_ids": _record_source_ids(owners, entry_by_id),
            "status": _record_status(owners, rebuild),
        }
        for (collection, record_id), owners in ordered[
            :MAX_INLINE_RECORD_DEPENDENCY_ROWS
        ]
    ]
    return rows, len(ordered)


def _stored_incremental_manifest(
    reuse_config: IncrementalReuseConfig,
    source_entries: list[dict[str, object]],
    record_dependencies: list[dict[str, object]],
    record_dependencies_total: int,
) -> dict[str, object] | None:
    """A manifest too large to store is not a reason to refuse the generation.

    It only buys the next pass its reuse, so the generation is built without one
    and the pass after this starts from a full build instead. Up to v4 that was
    not a rare branch but the only branch, because the ceiling was a constant
    the corpus had already passed. The ceiling is now the largest artifact the
    catalog will register at all, so this returns None only for a generation
    that could not have been published anyway.
    """
    manifest = {
        "version": INCREMENTAL_MANIFEST_VERSION,
        "reuse_config": asdict(reuse_config),
        "sources": source_entries,
        "record_dependencies": record_dependencies,
        "record_dependencies_total": record_dependencies_total,
    }
    if len(canonical_json_bytes(manifest)) > MAX_STORED_INCREMENTAL_MANIFEST_BYTES:
        return None
    return manifest


def _incremental_extractor_version(
    snapshot: corpus_snapshot.CorpusSnapshot | None, reuse_config: IncrementalReuseConfig
) -> str:
    if snapshot is None:
        return reuse_config.extractor_version
    return snapshot.extractor_version
