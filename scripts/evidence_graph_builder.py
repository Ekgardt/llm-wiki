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
from repository_scope import RepositoryScope

GRAPH_SCHEMA_VERSION = evidence_graph.GRAPH_SCHEMA_VERSION
DEFAULT_GRAPH_EXTRACTOR_VERSION = "graph-extractor/v1"
DEFAULT_TOKENIZER_VERSION = "tokenizer/v1"
DEFAULT_TOKENIZER_CONFIG_SHA256 = "0" * 64
CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v1"
COMPLETE_CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v2"
INCREMENTAL_MANIFEST_VERSION = "evidence-graph-incremental/v4"
_LEGACY_INCREMENTAL_MANIFEST_VERSIONS = frozenset(
    {
        "evidence-graph-incremental/v1",
        "evidence-graph-incremental/v2",
        "evidence-graph-incremental/v3",
    }
)
MAX_INCREMENTAL_MANIFEST_BYTES = 64 * 1024 * 1024
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
        for name in ("extractor_version", "grammar_version", "compiler_version", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError(f"{name} must be a bounded non-empty string")
        for name in ("resolver_config_sha256", "workspace_manifest_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
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
) -> Mapping[str, object]:
    if graph_schema is evidence_graph.GraphSchema.V3 and code_capture is None:
        raise ValueError("evidence-graph/v3 manifests require code_capture")
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
    if incremental_manifest_bytes is not None:
        artifacts.append(
            {
                "path": "incremental-manifest.json",
                "size": len(incremental_manifest_bytes),
                "sha256": hashlib.sha256(incremental_manifest_bytes).hexdigest(),
            }
        )
    if search_artifact is not None:
        artifacts.append(dict(search_artifact))
    artifacts.sort(key=lambda item: str(item["path"]))
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
        "artifacts": artifacts,
        "vector_state": "absent",
        **({"parent_generation_id": parent_generation_id} if parent_generation_id else {}),
        **({"repository_scope": repository_scope.as_dict()} if repository_scope else {}),
    }
    if code_capture is not None:
        from code_workspace import code_capture_as_dict

        manifest["code_capture"] = code_capture_as_dict(code_capture)
    return manifest


def _check_stop(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if bool(cancelled and cancelled()):
        raise TimeoutError("Evidence Graph build cancelled")
    if deadline is not None and time.monotonic() >= deadline:
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
        if not isinstance(content, bytes):
            raise TypeError("captured source content must be bytes")
        if source.get("size") != len(content) or source.get("sha256") != _hash_bytes(
            content, deadline=deadline, cancelled=cancelled
        ):
            raise ValueError("captured source size or hash does not match source bytes")
        snapshot[source_id] = content
    return snapshot


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
    if not isinstance(catalog, generation_catalog.GenerationCatalog):
        raise TypeError("catalog must be a GenerationCatalog")
    if not isinstance(graph_schema, evidence_graph.GraphSchema):
        raise TypeError("graph_schema must be a GraphSchema")
    if repository_scope is not None and not isinstance(repository_scope, RepositoryScope):
        raise TypeError("repository_scope must be a RepositoryScope or None")
    if repository_scope is not None:
        repository_scope = RepositoryScope.from_dict(repository_scope.as_dict())
    if snapshot is not None and not isinstance(snapshot, corpus_snapshot.CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot or None")
    if code_capture is not None and not isinstance(
        code_capture, corpus_snapshot.CodeCaptureContract
    ):
        raise TypeError("code_capture must be a CodeCaptureContract or None")
    if code_capture is not None:
        from code_workspace import code_capture_as_dict, validate_code_capture

        validate_code_capture(code_capture_as_dict(code_capture))
    if snapshot is not None and code_capture is not None and snapshot.code_capture != code_capture:
        raise ValueError("code_capture must be the exact supplied CorpusSnapshot contract")
    if snapshot is not None and activate and publication_root is None:
        raise ValueError("complete generation activation requires publication_root")
    if graph_schema is evidence_graph.GraphSchema.V3 and (
        snapshot is None or repository_scope is None or code_capture is None
    ):
        raise ValueError(
            "evidence-graph/v3 requires a CorpusSnapshot, repository scope, and code_capture"
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
    if code_capture is not None:
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

    if snapshot is not None:
        snapshot_rows = [
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
        snapshot_bytes = {
            source.record.logical_id: source.content for source in snapshot.sources
        }
        if sources_list != snapshot_rows or source_bytes_snapshot != snapshot_bytes:
            raise ValueError("builder sources must be the exact supplied CorpusSnapshot")
        if (
            collector_version != snapshot.collector_version
            or extractor_version != snapshot.extractor_version
        ):
            raise ValueError("corpus provenance must match the supplied CorpusSnapshot")

    # 1. Snapshot source membership and exact source SHA-256 hashes BEFORE
    # any extraction. The hash pins the source manifest in the generation
    # manifest so post-build validation can detect drift.
    if snapshot is None:
        source_manifest, source_manifest_bytes, source_manifest_sha256 = _snapshot_source_manifest(
            sources_list,
            policy=policy,
            collector_version=collector_version,
            extractor_version=extractor_version,
        )
    else:
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
    _check_stop(deadline, cancelled)

    verified_analysis_list: list[VerifiedAnalysisBatch] = []
    for batch in verified_analyses:
        _check_stop(deadline, cancelled)
        if len(verified_analysis_list) >= evidence_graph.MAX_VALIDATION_ROWS:
            raise ValueError("verified analysis row ceiling exceeded")
        if type(batch) is not VerifiedAnalysisBatch:
            raise TypeError("verified_analyses must contain VerifiedAnalysisBatch values")
        if batch.source_manifest_sha256 != source_manifest_sha256:
            raise ValueError("verified analysis source manifest must match generation manifest")
        if graph_schema is evidence_graph.GraphSchema.V3 and (
            batch.analysis.run.repository_id,
            batch.analysis.run.checkout_id,
        ) != (repository_scope.repository_id, repository_scope.checkout_id):
            raise ValueError("verified analysis repository or checkout does not match publication")
        verified_analysis_list.append(batch)
    if verified_analysis_list and code_capture is None:
        raise ValueError("verified analyses require code_capture")

    nodes_list = _materialize(
        nodes,
        label="nodes",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )
    occurrences_list = _materialize(
        occurrences,
        label="occurrences",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )
    assertions_list = _materialize(
        assertions,
        label="assertions",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )
    evidence_list = _materialize(
        evidence,
        label="evidence",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )
    observations_list = _materialize(
        observations,
        label="observations",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )
    dependencies_list = _materialize(
        dependencies,
        label="dependencies",
        limit=evidence_graph.MAX_VALIDATION_ROWS,
        deadline=deadline,
        cancelled=cancelled,
    )

    if kill_point == "before_directory_create":
        raise KillPointError(kill_point)

    generation_path = catalog.generations_path / generation_id
    if generation_path.exists() or generation_path.is_symlink():
        raise FileExistsError(f"generation {generation_id!r} already exists; builder never mutates")
    generation_path.mkdir(parents=True, exist_ok=False)
    fsync_directory(generation_path)

    # Track whether we've reached the registration phase. Kill-point aborts
    # leave partial state on disk (they simulate a crash); any other
    # exception during artifact build cleans up so retries are not blocked.
    publication_attempted = False
    try:
        if kill_point == "during_extraction":
            raise KillPointError(kill_point)

        database_path = generation_path / "evidence.sqlite3"
        evidence_graph.create_generation_database(
            database_path,
            schema=graph_schema,
            sources=sources_list,
            source_bytes=source_bytes_snapshot,
            nodes=nodes_list,
            occurrences=occurrences_list,
            assertions=assertions_list,
            evidence=evidence_list,
            observations=observations_list,
            dependencies=dependencies_list,
            verified_analyses=verified_analysis_list,
            publication_generation_id=(
                generation_id if graph_schema is evidence_graph.GraphSchema.V3 else None
            ),
            publication_expected_active=(
                expected_active if graph_schema is evidence_graph.GraphSchema.V3 else None
            ),
            repository_scope=(
                repository_scope if graph_schema is evidence_graph.GraphSchema.V3 else None
            ),
            deadline=deadline,
            cancelled=cancelled,
        )
        _check_stop(deadline, cancelled)
        fsync_file(database_path)
        fsync_directory(generation_path)

        search_artifact = None
        if snapshot is not None:
            import search_memory

            search_artifact = search_memory.build_generation_fts(
                snapshot,
                generation_path,
                deadline=deadline,
                cancelled=cancelled,
            )

        if kill_point == "after_database_commit":
            raise KillPointError(kill_point)

        # 4. Write canonical source-manifest.json and manifest.json. Both
        # files are canonical JSON, fsynced, and the parent directory is
        # fsynced so the catalog can validate them durably.
        source_manifest_path = generation_path / "source-manifest.json"
        _write_canonical_file(
            source_manifest_path,
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
            schema_version=(
                COMPLETE_CORPUS_GENERATION_SCHEMA_VERSION
                if snapshot is not None
                else CORPUS_GENERATION_SCHEMA_VERSION
            ),
            tokenizer_version=(
                search_memory.GENERATION_TOKENIZER_VERSION
                if snapshot is not None
                else DEFAULT_TOKENIZER_VERSION
            ),
            tokenizer_config_sha256=(
                search_memory.GENERATION_TOKENIZER_CONFIG_SHA256
                if snapshot is not None
                else DEFAULT_TOKENIZER_CONFIG_SHA256
            ),
            search_artifact=search_artifact,
            incremental_manifest_bytes=incremental_manifest_bytes,
            code_capture=code_capture,
        )
        manifest_path = generation_path / "manifest.json"
        _write_canonical_file(manifest_path, manifest, deadline=deadline, cancelled=cancelled)
        fsync_directory(generation_path)

        # 5. Perform semantic validation once and carry the resulting
        # process-local capability through registration and activation.
        candidate = catalog._validate_candidate(  # noqa: SLF001
            generation_id,
            expected_repository_scope=repository_scope,
            deadline=deadline,
            cancelled=cancelled,
        )

        if kill_point == "after_validation":
            raise KillPointError(kill_point)

        # 6. Register the new generation. The catalog re-validates the
        # manifest and the on-disk seal before recording it; identical
        # retries are idempotent.
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

        if snapshot is None:
            catalog._register_validated(  # noqa: SLF001
                candidate, deadline=deadline, cancelled=cancelled
            )
            if kill_point == "before_activation":
                raise KillPointError(kill_point)
            activated = catalog._activate_validated(  # noqa: SLF001
                candidate,
                expected_active=expected_active,
                deadline=deadline,
                cancelled=cancelled,
            )
            if not activated:
                catalog.discard_unactivated(
                    generation_id, deadline=deadline, cancelled=cancelled
                )
        else:
            import search_memory

            if kill_point == "before_activation":
                raise KillPointError(kill_point)
            publication_attempted = True
            activated = search_memory._publish_validated_generation(  # noqa: SLF001
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

        result = BuildResult(
            generation_id=generation_id,
            generation_path=generation_path,
            manifest=manifest,
            activated=activated,
        )

        if kill_point == "after_activation":
            raise KillPointError(kill_point)

        return result
    except KillPointError:
        # Kill-point aborts deliberately leave the partial state on disk
        # so tests can verify the catalog ignores orphans and the prior
        # generation stays readable.
        raise
    except BaseException:
        cleanup_options = {}
        if publication_attempted:
            cleanup_options = {"deadline": deadline, "cancelled": cancelled}
        try:
            catalog.discard_unactivated(generation_id, **cleanup_options)
        except BaseException:
            # Preserve the publication failure. Catalog cleanup is fail-safe:
            # inability to prove the generation unreferenced leaves it on disk.
            pass
        raise


def _validated_extraction(value: object) -> SourceExtraction:
    if not isinstance(value, SourceExtraction):
        raise TypeError("extractor must return SourceExtraction")
    fingerprints = value.invalidation_fingerprints
    if not isinstance(fingerprints, Mapping) or set(fingerprints) != _INVALIDATION_KEYS:
        raise ValueError(
            "invalidation_fingerprints must contain exports, imports, signatures, aliases, "
            "and project_metadata"
        )
    if any(not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None for item in fingerprints.values()):
        raise ValueError("invalidation fingerprints must be lowercase SHA-256 digests")
    if (
        not isinstance(value.source_dependencies, tuple)
        or any(not isinstance(item, str) or not item for item in value.source_dependencies)
        or tuple(sorted(set(value.source_dependencies))) != value.source_dependencies
    ):
        raise ValueError("source_dependencies must be a sorted unique tuple of source IDs")
    if not isinstance(value.workspace_sensitive, bool):
        raise TypeError("workspace_sensitive must be a boolean")
    for collection in _RECORD_COLLECTIONS:
        records = getattr(value, collection)
        if not isinstance(records, tuple) or any(not isinstance(record, Mapping) for record in records):
            raise TypeError(f"{collection} must be a tuple of record mappings")
    return value


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
        max_bytes=MAX_INCREMENTAL_MANIFEST_BYTES,
    )
    _check_stop(deadline, cancelled)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("incremental manifest must contain valid UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or canonical_json_bytes(value) != raw:
        raise ValueError("incremental manifest must be a canonical JSON object")
    return _validated_incremental_manifest(value), generation_manifest


def _validated_incremental_manifest(value: Mapping[str, object]) -> Mapping[str, object]:
    if set(value) != {"version", "reuse_config", "sources", "record_dependencies"}:
        raise ValueError("incremental manifest must be a closed object")
    version = value["version"]
    if version not in {
        INCREMENTAL_MANIFEST_VERSION,
        *_LEGACY_INCREMENTAL_MANIFEST_VERSIONS,
    }:
        raise ValueError("incremental manifest has an unsupported version")
    config = value["reuse_config"]
    if not isinstance(config, Mapping) or set(config) != set(IncrementalReuseConfig.__dataclass_fields__):
        raise ValueError("incremental reuse config must be a closed object")
    IncrementalReuseConfig(**config)
    sources = value["sources"]
    if not isinstance(sources, list):
        raise TypeError("incremental manifest sources must be an array")
    seen_sources: set[str] = set()
    for entry in sources:
        entry_keys = {
            "source_id",
            "relative_path",
            "sha256",
            "source_dependencies",
            "invalidation_fingerprints",
            "records",
        }
        if version in {
            INCREMENTAL_MANIFEST_VERSION,
            "evidence-graph-incremental/v2",
            "evidence-graph-incremental/v3",
        }:
            entry_keys.add("language")
        if version == INCREMENTAL_MANIFEST_VERSION:
            entry_keys.add("workspace_sensitive")
        elif version == "evidence-graph-incremental/v3":
            entry_keys.add("workspace_sensitive_sources")
        if not isinstance(entry, Mapping) or set(entry) != entry_keys:
            raise ValueError("incremental source entries must be closed objects")
        source_id = entry["source_id"]
        if not isinstance(source_id, str) or not source_id or source_id in seen_sources:
            raise ValueError("incremental source IDs must be unique non-empty strings")
        seen_sources.add(source_id)
        if not isinstance(entry["relative_path"], str) or not entry["relative_path"]:
            raise ValueError("incremental source paths must be non-empty strings")
        if version in {
            INCREMENTAL_MANIFEST_VERSION,
            "evidence-graph-incremental/v2",
            "evidence-graph-incremental/v3",
        } and not (
            entry["language"] is None or isinstance(entry["language"], str)
        ):
            raise ValueError("incremental source language must be a string or null")
        if not isinstance(entry["sha256"], str) or _SHA256_RE.fullmatch(entry["sha256"]) is None:
            raise ValueError("incremental source hashes must be lowercase SHA-256 digests")
        dependencies = entry["source_dependencies"]
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or dependencies != sorted(set(dependencies))
        ):
            raise ValueError("incremental source dependencies must be sorted and unique")
        if version == INCREMENTAL_MANIFEST_VERSION:
            if not isinstance(entry["workspace_sensitive"], bool):
                raise TypeError("incremental workspace_sensitive must be a boolean")
        elif version == "evidence-graph-incremental/v3":
            sensitive_sources = entry["workspace_sensitive_sources"]
            if (
                not isinstance(sensitive_sources, list)
                or any(not isinstance(item, str) or not item for item in sensitive_sources)
                or sensitive_sources != sorted(set(sensitive_sources))
                or len(sensitive_sources) > MAX_LEGACY_WORKSPACE_SENSITIVE_SOURCES
            ):
                raise ValueError(
                    "incremental workspace-sensitive sources must be bounded, sorted, and unique"
                )
        fingerprints = entry["invalidation_fingerprints"]
        if (
            not isinstance(fingerprints, Mapping)
            or set(fingerprints) != _INVALIDATION_KEYS
            or any(
                not isinstance(item, str) or _SHA256_RE.fullmatch(item) is None
                for item in fingerprints.values()
            )
        ):
            raise ValueError("incremental invalidation fingerprints are malformed")
        records = entry["records"]
        if not isinstance(records, Mapping) or set(records) != set(_RECORD_COLLECTIONS):
            raise ValueError("incremental source record membership must be a closed object")
        for record_ids in records.values():
            if (
                not isinstance(record_ids, list)
                or any(not isinstance(item, str) or not item for item in record_ids)
                or record_ids != sorted(set(record_ids))
            ):
                raise ValueError("incremental record IDs must be sorted and unique")
    if not isinstance(value["record_dependencies"], list):
        raise TypeError("incremental record dependencies must be an array")
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


def _row_record(collection: str, row: sqlite3.Row) -> dict[str, object]:
    if collection == "nodes":
        return {
            "node_id": row["node_id"],
            "kind": row["kind"],
            "identity_scheme": row["identity_scheme"],
            "identity_key": row["identity_key"],
            "metadata": json.loads(row["metadata_json"]),
        }
    if collection == "occurrences":
        return dict(row)
    if collection == "assertions":
        record = dict(row)
        record["literal"] = (
            None if record.pop("literal_json") is None else json.loads(row["literal_json"])
        )
        return record
    if collection == "evidence" or collection == "observations" or collection == "dependencies":
        return dict(row)
    raise AssertionError(f"unknown record collection: {collection}")


def _parent_records(
    database_path: Path,
    source_entries: Mapping[str, Mapping[str, object]],
    reused_sources: set[str],
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, dict[str, Mapping[str, object]]]:
    wanted = {
        collection: {
            str(record_id)
            for source_id in reused_sources
            for record_id in source_entries[source_id]["records"][collection]
        }
        for collection in _RECORD_COLLECTIONS
    }
    loaded: dict[str, dict[str, Mapping[str, object]]] = {
        collection: {} for collection in _RECORD_COLLECTIONS
    }
    uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=0)) as database:
        database.row_factory = sqlite3.Row
        database.set_progress_handler(
            lambda: int(
                bool(cancelled and cancelled())
                or (deadline is not None and time.monotonic() >= deadline)
            ),
            evidence_graph.PROGRESS_OPCODES,
        )
        try:
            for collection in _RECORD_COLLECTIONS:
                table = "dependency" if collection == "dependencies" else collection.removesuffix("s")
                key = _RECORD_KEYS[collection]
                for row in database.execute(f"SELECT * FROM {table} ORDER BY {key}"):
                    _check_stop(deadline, cancelled)
                    record_id = str(row[key])
                    if record_id in wanted[collection]:
                        loaded[collection][record_id] = _row_record(collection, row)
        except sqlite3.OperationalError as exc:
            if bool(cancelled and cancelled()) or (
                deadline is not None and time.monotonic() >= deadline
            ):
                raise TimeoutError("incremental parent read cancelled or deadline reached") from exc
            raise
        finally:
            database.set_progress_handler(None, 0)
    for collection in _RECORD_COLLECTIONS:
        if set(loaded[collection]) != wanted[collection]:
            raise ValueError("incremental manifest references missing parent records")
    return loaded


def _renames(
    previous: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
    added: set[str],
    deleted: set[str],
) -> tuple[tuple[str, str], ...]:
    old_by_hash: dict[str, list[str]] = {}
    new_by_hash: dict[str, list[str]] = {}
    for source_id in deleted:
        old_by_hash.setdefault(str(previous[source_id]["sha256"]), []).append(source_id)
    for source_id in added:
        new_by_hash.setdefault(str(current[source_id]["sha256"]), []).append(source_id)
    pairs = []
    pairs.extend(
        (source_id, source_id)
        for source_id in sorted(previous.keys() & current.keys())
        if previous[source_id].get("sha256") == current[source_id].get("sha256")
        and previous[source_id].get("relative_path") != current[source_id].get("relative_path")
    )
    for digest in sorted(old_by_hash.keys() & new_by_hash.keys()):
        old = sorted(old_by_hash[digest])
        new = sorted(new_by_hash[digest])
        pairs.extend(zip(old, new, strict=False))
    return tuple(sorted(pairs))


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
        for source_id in owners:
            grouped[source_id][collection].append(record_id)
    for records in grouped.values():
        _check_stop(deadline, cancelled)
        for record_ids in records.values():
            record_ids.sort()
    return grouped


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
    if not isinstance(catalog, generation_catalog.GenerationCatalog):
        raise TypeError("catalog must be a GenerationCatalog")
    if not isinstance(reuse_config, IncrementalReuseConfig):
        raise TypeError("reuse_config must be IncrementalReuseConfig")
    if not callable(extractor):
        raise TypeError("extractor must be callable")
    if repository_scope is not None and not isinstance(repository_scope, RepositoryScope):
        raise TypeError("repository_scope must be a RepositoryScope or None")
    if repository_scope is not None:
        repository_scope = RepositoryScope.from_dict(repository_scope.as_dict())
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
    current = {str(source["source_id"]): source for source in sources_list}
    if len(current) != len(sources_list):
        raise ValueError("captured sources must have unique source IDs")
    immutable_sources = tuple(MappingProxyType(source) for source in sources_list)
    immutable_source_by_id = {
        str(source["source_id"]): source for source in immutable_sources
    }
    immutable_source_bytes = MappingProxyType(source_snapshot)

    parent_manifest = None
    parent_sources: dict[str, Mapping[str, object]] = {}
    parent_entries: dict[str, Mapping[str, object]] = {}
    config_matches = False
    if parent_generation_id is not None:
        parent_manifest, parent_generation_manifest = _load_incremental_manifest(
            catalog,
            parent_generation_id,
            deadline=deadline,
            cancelled=cancelled,
        )
        if parent_manifest is not None:
            parent_entries = {
                str(entry["source_id"]): entry for entry in parent_manifest.get("sources", [])
            }
            parent_sources = parent_entries
            parent_config = parent_manifest.get("reuse_config")
            config_matches = (
                parent_manifest.get("version") == INCREMENTAL_MANIFEST_VERSION
                and isinstance(parent_config, Mapping)
                and {
                    key: value
                    for key, value in parent_config.items()
                    if key != "workspace_manifest_sha256"
                }
                == {
                    key: value
                    for key, value in asdict(reuse_config).items()
                    if key != "workspace_manifest_sha256"
                }
                and parent_generation_manifest is not None
                and parent_generation_manifest.get("repository_scope")
                == repository_scope_object
            )

    current_ids = set(current)
    previous_ids = set(parent_sources)
    added = current_ids - previous_ids
    deleted = previous_ids - current_ids
    changed = {
        source_id
        for source_id in current_ids & previous_ids
        if (
            current[source_id]["sha256"] != parent_sources[source_id].get("sha256")
            or current[source_id]["relative_path"] != parent_sources[source_id].get("relative_path")
            or current[source_id].get("language") != parent_sources[source_id].get("language")
        )
    }
    renamed = _renames(parent_sources, current, added, deleted)
    current_workspace_ids = {
        source_id
        for source_id, source in current.items()
        if not str(source["relative_path"]).startswith("knowledge/")
    }
    previous_workspace_ids = {
        source_id
        for source_id, source in parent_sources.items()
        if not str(source["relative_path"]).startswith("knowledge/")
    }
    workspace_ids = current_workspace_ids | previous_workspace_ids
    workspace_membership_changed = False
    if config_matches:
        parent_workspace_manifest = str(
            parent_manifest["reuse_config"]["workspace_manifest_sha256"]
        )
        workspace_membership_changed = bool(
            parent_workspace_manifest != reuse_config.workspace_manifest_sha256
            or added & current_workspace_ids
            or deleted & previous_workspace_ids
            or any(
                source_id in workspace_ids
                and (
                    current[source_id]["relative_path"]
                    != parent_sources[source_id].get("relative_path")
                    or current[source_id].get("language")
                    != parent_sources[source_id].get("language")
                )
                for source_id in changed
            )
        )
    rebuild = set(current_ids if not config_matches else added | changed)
    if workspace_membership_changed:
        rebuild.update(current_workspace_ids)
    extracted: dict[str, SourceExtraction] = {}

    def extract(source_id: str) -> SourceExtraction:
        _check_stop(deadline, cancelled)
        result = _validated_extraction(
            extractor(
                immutable_source_by_id[source_id],
                source_snapshot[source_id],
                sources=immutable_sources,
                source_bytes=immutable_source_bytes,
                deadline=deadline,
                cancelled=cancelled,
            )
        )
        unknown = set(result.source_dependencies) - current_ids
        if unknown:
            raise ValueError(f"source extraction has unknown dependencies: {sorted(unknown)!r}")
        extracted[source_id] = result
        return result

    for source_id in sorted(rebuild):
        extract(source_id)

    if config_matches:
        semantic_changes = {
            source_id
            for source_id in changed
            if dict(extracted[source_id].invalidation_fingerprints or {})
            != parent_entries[source_id].get("invalidation_fingerprints")
        }
        workspace_surface_changed = bool(
            semantic_changes and not workspace_membership_changed
        )
        workspace_invalidated = (
            (_workspace_sensitive_source_ids(parent_entries) & current_ids) - rebuild
            if workspace_surface_changed
            else set()
        )
        for source_id in sorted(workspace_invalidated):
            extract(source_id)
        rebuild.update(workspace_invalidated)
        invalidated = set(semantic_changes | deleted | workspace_invalidated)
        while invalidated:
            newly_invalidated = {
                source_id
                for source_id in current_ids - rebuild
                if set(parent_entries[source_id].get("source_dependencies", ())) & invalidated
            }
            if not newly_invalidated:
                break
            for source_id in sorted(newly_invalidated):
                extract(source_id)
            rebuild.update(newly_invalidated)
            invalidated = newly_invalidated

    reused = current_ids - rebuild
    parent_records = (
        _parent_records(
            catalog.generations_path / parent_generation_id / "evidence.sqlite3",
            parent_entries,
            reused,
            deadline=deadline,
            cancelled=cancelled,
        )
        if reused and parent_generation_id is not None
        else {collection: {} for collection in _RECORD_COLLECTIONS}
    )
    merged: dict[str, dict[str, Mapping[str, object]]] = {
        collection: dict(parent_records[collection]) for collection in _RECORD_COLLECTIONS
    }
    ownership: dict[tuple[str, str], set[str]] = {}
    for source_id in reused:
        for collection in _RECORD_COLLECTIONS:
            for record_id in parent_entries[source_id]["records"][collection]:
                ownership.setdefault((collection, str(record_id)), set()).add(source_id)
    for source_id in sorted(rebuild):
        result = extracted[source_id]
        for collection in _RECORD_COLLECTIONS:
            key = _RECORD_KEYS[collection]
            for record in getattr(result, collection):
                record_id = str(record[key])
                existing = merged[collection].get(record_id)
                candidate = dict(record)
                if existing is not None and existing != candidate:
                    raise ValueError(f"conflicting {collection} record {record_id!r}")
                merged[collection][record_id] = candidate
                ownership.setdefault((collection, record_id), set()).add(source_id)

    records_by_owner = _record_ids_by_owner(
        ownership,
        sorted(current_ids),
        deadline=deadline,
        cancelled=cancelled,
    )
    source_entries = []
    for source_id in sorted(current_ids):
        result = extracted.get(source_id)
        if result is None:
            entry = parent_entries[source_id]
            source_dependencies = list(entry["source_dependencies"])
            fingerprints = dict(entry["invalidation_fingerprints"])
            workspace_sensitive = bool(entry["workspace_sensitive"])
        else:
            source_dependencies = list(result.source_dependencies)
            fingerprints = dict(result.invalidation_fingerprints or {})
            workspace_sensitive = result.workspace_sensitive
        source_entries.append(
            {
                "source_id": source_id,
                "relative_path": str(current[source_id]["relative_path"]),
                "sha256": str(current[source_id]["sha256"]),
                "source_dependencies": source_dependencies,
                "invalidation_fingerprints": fingerprints,
                "records": records_by_owner[source_id],
                **(
                    {"language": current[source_id].get("language")}
                    if INCREMENTAL_MANIFEST_VERSION
                    in {
                        "evidence-graph-incremental/v2",
                        "evidence-graph-incremental/v3",
                        "evidence-graph-incremental/v4",
                    }
                    else {}
                ),
                **(
                    {"workspace_sensitive": workspace_sensitive}
                    if INCREMENTAL_MANIFEST_VERSION == "evidence-graph-incremental/v4"
                    else {
                        "workspace_sensitive_sources": []
                    }
                    if INCREMENTAL_MANIFEST_VERSION == "evidence-graph-incremental/v3"
                    else {}
                ),
            }
        )
    entry_by_id = {str(entry["source_id"]): entry for entry in source_entries}
    record_dependencies = []
    for (collection, record_id), owners in sorted(ownership.items()):
        dependencies = set(owners)
        for owner in owners:
            dependencies.update(map(str, entry_by_id[owner]["source_dependencies"]))
        record_dependencies.append(
            {
                "collection": collection,
                "record_id": record_id,
                "source_ids": sorted(dependencies),
                "status": "rebuilt" if owners & rebuild else "reused",
            }
        )
    incremental_manifest = {
        "version": INCREMENTAL_MANIFEST_VERSION,
        "reuse_config": asdict(reuse_config),
        "sources": source_entries,
        "record_dependencies": record_dependencies,
    }
    if len(canonical_json_bytes(incremental_manifest)) > MAX_INCREMENTAL_MANIFEST_BYTES:
        raise ValueError("incremental manifest exceeds the supported byte ceiling")
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
        extractor_version=(
            snapshot.extractor_version if snapshot is not None else reuse_config.extractor_version
        ),
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
        added_sources=tuple(sorted(added)),
        changed_sources=tuple(sorted(changed)),
        deleted_sources=tuple(sorted(deleted)),
        renamed_sources=renamed,
        reused_sources=tuple(sorted(reused)),
        rebuilt_sources=tuple(sorted(rebuild)),
    )
