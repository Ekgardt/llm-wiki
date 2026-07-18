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
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import corpus_snapshot
import evidence_graph
import generation_catalog
from reliable_memory import canonical_json_bytes, fsync_directory, fsync_file, read_runtime_bytes

GRAPH_SCHEMA_VERSION = evidence_graph.GRAPH_SCHEMA_VERSION
DEFAULT_GRAPH_EXTRACTOR_VERSION = "graph-extractor/v1"
DEFAULT_TOKENIZER_VERSION = "tokenizer/v1"
DEFAULT_TOKENIZER_CONFIG_SHA256 = "0" * 64
CORPUS_GENERATION_SCHEMA_VERSION = "corpus-generation/v1"
INCREMENTAL_MANIFEST_VERSION = "evidence-graph-incremental/v1"
MAX_INCREMENTAL_MANIFEST_BYTES = 64 * 1024 * 1024
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
    incremental_manifest_bytes: bytes | None = None,
) -> Mapping[str, object]:
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
    artifacts.sort(key=lambda item: str(item["path"]))
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
        "artifacts": artifacts,
        "vector_state": "absent",
        **({"parent_generation_id": parent_generation_id} if parent_generation_id else {}),
    }


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

    sources_list = [
        dict(source)
        for source in _materialize(sources, deadline=deadline, cancelled=cancelled)
    ]
    source_bytes_snapshot = _verify_source_snapshot(
        sources_list, source_bytes, deadline=deadline, cancelled=cancelled
    )

    # 1. Snapshot source membership and exact source SHA-256 hashes BEFORE
    # any extraction. The hash pins the source manifest in the generation
    # manifest so post-build validation can detect drift.
    source_manifest, source_manifest_bytes, source_manifest_sha256 = _snapshot_source_manifest(
        sources_list,
        policy=policy,
        collector_version=collector_version,
        extractor_version=extractor_version,
    )
    _check_stop(deadline, cancelled)

    nodes_list = _materialize(nodes, deadline=deadline, cancelled=cancelled)
    occurrences_list = _materialize(occurrences, deadline=deadline, cancelled=cancelled)
    assertions_list = _materialize(assertions, deadline=deadline, cancelled=cancelled)
    evidence_list = _materialize(evidence, deadline=deadline, cancelled=cancelled)
    observations_list = _materialize(observations, deadline=deadline, cancelled=cancelled)
    dependencies_list = _materialize(dependencies, deadline=deadline, cancelled=cancelled)

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
    registered = False
    try:
        if kill_point == "during_extraction":
            raise KillPointError(kill_point)

        database_path = generation_path / "evidence.sqlite3"
        evidence_graph.create_generation_database(
            database_path,
            sources=sources_list,
            source_bytes=source_bytes_snapshot,
            nodes=nodes_list,
            occurrences=occurrences_list,
            assertions=assertions_list,
            evidence=evidence_list,
            observations=observations_list,
            dependencies=dependencies_list,
            deadline=deadline,
            cancelled=cancelled,
        )
        _check_stop(deadline, cancelled)
        fsync_file(database_path)
        fsync_directory(generation_path)

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
            incremental_manifest_bytes=incremental_manifest_bytes,
        )
        manifest_path = generation_path / "manifest.json"
        _write_canonical_file(manifest_path, manifest, deadline=deadline, cancelled=cancelled)
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
            cancelled=cancelled,
        )

        if kill_point == "after_validation":
            raise KillPointError(kill_point)

        # 6. Register the new generation. The catalog re-validates the
        # manifest and the on-disk seal before recording it; identical
        # retries are idempotent.
        catalog.register(generation_id, deadline=deadline, cancelled=cancelled)
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
        if not registered:
            # Validation / artifact failure: clean up the partial directory
            # so a retry is not blocked by an orphan that the caller never
            # asked for. Registered generations stay — the catalog owns
            # them and the caller can retry activation explicitly.
            import shutil

            shutil.rmtree(generation_path, ignore_errors=True)
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
) -> Mapping[str, object] | None:
    _check_stop(deadline, cancelled)
    generation_path = catalog.generations_path / generation_id
    if not (generation_path / "incremental-manifest.json").exists():
        return None
    generation_catalog._validate_generation(  # noqa: SLF001 - reuse must validate the sealed parent
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
    return _validated_incremental_manifest(value)


def _validated_incremental_manifest(value: Mapping[str, object]) -> Mapping[str, object]:
    if set(value) != {"version", "reuse_config", "sources", "record_dependencies"}:
        raise ValueError("incremental manifest must be a closed object")
    if value["version"] != INCREMENTAL_MANIFEST_VERSION:
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
        if not isinstance(entry, Mapping) or set(entry) != {
            "source_id",
            "relative_path",
            "sha256",
            "source_dependencies",
            "invalidation_fingerprints",
            "records",
        }:
            raise ValueError("incremental source entries must be closed objects")
        source_id = entry["source_id"]
        if not isinstance(source_id, str) or not source_id or source_id in seen_sources:
            raise ValueError("incremental source IDs must be unique non-empty strings")
        seen_sources.add(source_id)
        if not isinstance(entry["relative_path"], str) or not entry["relative_path"]:
            raise ValueError("incremental source paths must be non-empty strings")
        if not isinstance(entry["sha256"], str) or _SHA256_RE.fullmatch(entry["sha256"]) is None:
            raise ValueError("incremental source hashes must be lowercase SHA-256 digests")
        dependencies = entry["source_dependencies"]
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or dependencies != sorted(set(dependencies))
        ):
            raise ValueError("incremental source dependencies must be sorted and unique")
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
    with sqlite3.connect(uri, uri=True, timeout=0) as database:
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
) -> IncrementalBuildResult:
    """Build a complete immutable generation while reusing exact parent records."""
    if not isinstance(catalog, generation_catalog.GenerationCatalog):
        raise TypeError("catalog must be a GenerationCatalog")
    if not isinstance(reuse_config, IncrementalReuseConfig):
        raise TypeError("reuse_config must be IncrementalReuseConfig")
    if not callable(extractor):
        raise TypeError("extractor must be callable")
    sources_list = [
        dict(source)
        for source in _materialize(sources, deadline=deadline, cancelled=cancelled)
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
        parent_manifest = _load_incremental_manifest(
            catalog,
            parent_generation_id,
            deadline=deadline,
            cancelled=cancelled,
        )
        if parent_manifest is not None:
            if parent_manifest.get("version") != INCREMENTAL_MANIFEST_VERSION:
                raise ValueError("incremental manifest has an unsupported version")
            parent_entries = {
                str(entry["source_id"]): entry for entry in parent_manifest.get("sources", [])
            }
            parent_sources = parent_entries
            config_matches = parent_manifest.get("reuse_config") == asdict(reuse_config)

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
        )
    }
    renamed = _renames(parent_sources, current, added, deleted)
    rebuild = set(current_ids if not config_matches else added | changed)
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
        invalidated = set(semantic_changes | deleted)
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

    source_entries = []
    for source_id in sorted(current_ids):
        result = extracted.get(source_id)
        if result is None:
            entry = parent_entries[source_id]
            source_dependencies = list(entry["source_dependencies"])
            fingerprints = dict(entry["invalidation_fingerprints"])
        else:
            source_dependencies = list(result.source_dependencies)
            fingerprints = dict(result.invalidation_fingerprints or {})
        source_entries.append(
            {
                "source_id": source_id,
                "relative_path": str(current[source_id]["relative_path"]),
                "sha256": str(current[source_id]["sha256"]),
                "source_dependencies": source_dependencies,
                "invalidation_fingerprints": fingerprints,
                "records": {
                    collection: sorted(
                        record_id
                        for (record_collection, record_id), owners in ownership.items()
                        if record_collection == collection and source_id in owners
                    )
                    for collection in _RECORD_COLLECTIONS
                },
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
        extractor_version=reuse_config.extractor_version,
        graph_extractor_version=reuse_config.extractor_version,
        expected_active=expected_active,
        activate=activate,
        kill_point=kill_point,
        deadline=deadline,
        cancelled=cancelled,
        incremental_manifest=incremental_manifest,
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
