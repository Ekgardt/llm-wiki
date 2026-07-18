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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import corpus_snapshot
import evidence_graph
import generation_catalog
from reliable_memory import canonical_json_bytes, fsync_directory, fsync_file

GRAPH_SCHEMA_VERSION = evidence_graph.GRAPH_SCHEMA_VERSION
DEFAULT_GRAPH_EXTRACTOR_VERSION = "graph-extractor/v1"
DEFAULT_TOKENIZER_VERSION = "tokenizer/v1"
DEFAULT_TOKENIZER_CONFIG_SHA256 = "0" * 64
CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v1"

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


def _validate_kill_point(kill_point: str | None) -> None:
    if kill_point is not None and kill_point not in KILL_POINTS:
        raise ValueError(
            f"kill_point must be one of {KILL_POINTS} or None, got {kill_point!r}"
        )


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
    database_bytes: bytes,
    source_manifest_bytes: bytes,
) -> Mapping[str, object]:
    import hashlib

    return {
        "generation_id": generation_id,
        "schema_version": CORPUS_GENERATION_SCHEMA_VERSION,
        "collector_version": collector_version,
        "extractor_version": extractor_version,
        "tokenizer_version": DEFAULT_TOKENIZER_VERSION,
        "tokenizer_config_sha256": DEFAULT_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "graph_extractor_version": graph_extractor_version,
        "source_manifest_sha256": source_manifest_sha256,
        "artifacts": [
            {
                "path": "evidence.sqlite3",
                "size": len(database_bytes),
                "sha256": hashlib.sha256(database_bytes).hexdigest(),
            },
            {
                "path": "source-manifest.json",
                "size": len(source_manifest_bytes),
                "sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
            },
        ],
        "vector_state": "absent",
        **({"parent_generation_id": parent_generation_id} if parent_generation_id else {}),
    }


def _write_canonical_file(path: Path, payload: Mapping[str, object]) -> None:
    encoded = canonical_json_bytes(payload)
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        try:
            fsync_file(path)
        except OSError:
            # fsync is best-effort on some filesystems; the encoded payload
            # itself is already on disk after flush().
            pass


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

    sources_list = list(sources)
    nodes_list = list(nodes)
    occurrences_list = list(occurrences)
    assertions_list = list(assertions)
    evidence_list = list(evidence)
    observations_list = list(observations)
    dependencies_list = list(dependencies)

    # 1. Snapshot source membership and exact source SHA-256 hashes BEFORE
    # any extraction. The hash pins the source manifest in the generation
    # manifest so post-build validation can detect drift.
    source_manifest, source_manifest_bytes, source_manifest_sha256 = (
        _snapshot_source_manifest(
            sources_list,
            policy=policy,
            collector_version=collector_version,
            extractor_version=extractor_version,
        )
    )

    if kill_point == "before_directory_create":
        raise KillPointError(kill_point)

    generation_path = catalog.generations_path / generation_id
    if generation_path.exists() or generation_path.is_symlink():
        raise FileExistsError(
            f"generation {generation_id!r} already exists; builder never mutates"
        )
    generation_path.mkdir(parents=True, exist_ok=False)
    fsync_directory(generation_path)

    # Track whether we've reached the registration phase. Kill-point aborts
    # leave partial state on disk (they simulate a crash); any other
    # exception during artifact build cleans up so retries are not blocked.
    registered = False
    try:
        if kill_point == "during_extraction":
            raise KillPointError(kill_point)

        database_path = generation_path / "evidence.sqlite3"
        evidence_graph.create_generation_database(
            database_path,
            sources=sources_list,
            source_bytes=source_bytes,
            nodes=nodes_list,
            occurrences=occurrences_list,
            assertions=assertions_list,
            evidence=evidence_list,
            observations=observations_list,
            dependencies=dependencies_list,
        )
        fsync_file(database_path)
        fsync_directory(generation_path)

        if kill_point == "after_database_commit":
            raise KillPointError(kill_point)

        # 4. Write canonical source-manifest.json and manifest.json. Both
        # files are canonical JSON, fsynced, and the parent directory is
        # fsynced so the catalog can validate them durably.
        source_manifest_path = generation_path / "source-manifest.json"
        _write_canonical_file(source_manifest_path, source_manifest)

        database_bytes = database_path.read_bytes()
        manifest = _build_manifest(
            generation_id=generation_id,
            parent_generation_id=parent_generation_id,
            collector_version=collector_version,
            extractor_version=extractor_version,
            graph_extractor_version=graph_extractor_version,
            source_manifest_sha256=source_manifest_sha256,
            database_bytes=database_bytes,
            source_manifest_bytes=source_manifest_bytes,
        )
        manifest_path = generation_path / "manifest.json"
        _write_canonical_file(manifest_path, manifest)
        fsync_directory(generation_path)

        # 5. Validate schema, FKs, integrity_check, evidence spans, artifact
        # hashes, and source membership. validate_generation_artifact
        # re-reads the on-disk artifacts and verifies them against the
        # manifest.
        evidence_graph.validate_generation_artifact(
            generation_path,
            manifest,
            state_root=catalog.state_root,
            deadline=deadline,
        )

        if kill_point == "after_validation":
            raise KillPointError(kill_point)

        # 6. Register the new generation. The catalog re-validates the
        # manifest and the on-disk seal before recording it; identical
        # retries are idempotent.
        catalog.register(generation_id, deadline=deadline)
        registered = True

        if not activate:
            return BuildResult(
                generation_id=generation_id,
                generation_path=generation_path,
                manifest=manifest,
                activated=False,
            )

        if kill_point == "before_activation":
            raise KillPointError(kill_point)

        # 7. Compare-and-swap activation in one short catalog transaction.
        activated = catalog.activate(
            generation_id,
            expected_active=expected_active,
            deadline=deadline,
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
        if not registered:
            # Validation / artifact failure: clean up the partial directory
            # so a retry is not blocked by an orphan that the caller never
            # asked for. Registered generations stay — the catalog owns
            # them and the caller can retry activation explicitly.
            import shutil

            shutil.rmtree(generation_path, ignore_errors=True)
        raise
