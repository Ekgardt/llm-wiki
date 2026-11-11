"""Validating a whole generation and remembering the verdict."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from llm_wiki import generation_artifact_scan, generation_limits
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.generation_artifact_scan import (
    require_schema_contract,
    require_vector_contract,
    scan_artifacts,
)
from llm_wiki.generation_fields import (
    check_cancelled,
    check_monotonic_deadline,
    checked_generation_id,
)
from llm_wiki.generation_manifest import manifest_object, normalized_manifest
from llm_wiki.generation_seal import EntrySeal, content_seal, scan_generation, validate_directory
from reliable_memory import read_runtime_bytes


def _require_stable_scan(
    generation_path: Path,
    initial_seal: tuple[EntrySeal, ...],
    initial_files: set[str],
    seen: set[str],
    **stop: object,
) -> tuple[EntrySeal, ...]:
    final_seal, actual_files = scan_generation(generation_path, **stop)
    if final_seal != initial_seal:
        raise PermissionError("generation changed during validation")
    if initial_files != actual_files or actual_files != seen | {"manifest.json"}:
        raise ValueError("manifest must bind every generation artifact")
    return final_seal


# Semantic validation of a generation is a pure function of its bytes: the
# sources it is checked against come from the generation's own source manifest
# and evidence database, never from the live vault, and every call hashes every
# artifact against the manifest before the semantic check runs. The same bytes
# therefore cannot produce a different verdict — while on this vault one check
# cost 1.95 s and one query ran five of them, which put the 10-second MCP budget
# out of reach. See docs/research/2026-08-24-verify-the-same-bytes-once.md.
_MAX_REMEMBERED_VALIDATIONS = 8


# Verdicts by hashed identity: None is "valid", a string is the refusal the
# same bytes earned once. Remembering the refusal too is what keeps a
# generation that fails its content check from being re-derived on every
# question of every process — measured 2026-09-10 on the live vault, six
# validations and 40 s per search. See
# `docs/research/2026-09-10-a-generation-outlives-its-extractor.md`.
_VALIDATED_GENERATIONS: OrderedDict[tuple, str | None] = OrderedDict()


_VALIDATION_MEMORY_LOCK = threading.Lock()


_NOT_REMEMBERED = object()


def _validation_key(
    generation_path: Path,
    generation_id: str,
    manifest_digest: str,
    digests: dict[str, str],
    deep: bool = False,
) -> tuple:
    """Identity earned by hashing, not assumed: id, manifest, every artifact.

    The path is part of it so two vaults that happen to hold identical bytes are
    still each answered for themselves.
    """
    return (
        str(generation_path),
        generation_id,
        manifest_digest,
        tuple(sorted(digests.items())),
        # The depth is part of the identity: a shallow read must never satisfy a
        # later deep registration (2026-09-12).
        bool(deep),
    )


def _remembered_verdict(key: tuple) -> object:
    """None (valid), the remembered refusal, or `_NOT_REMEMBERED`."""
    with _VALIDATION_MEMORY_LOCK:
        if key not in _VALIDATED_GENERATIONS:
            return _NOT_REMEMBERED
        _VALIDATED_GENERATIONS.move_to_end(key)
        return _VALIDATED_GENERATIONS[key]


def _remember_verdict(key: tuple, refusal: str | None) -> None:
    with _VALIDATION_MEMORY_LOCK:
        _VALIDATED_GENERATIONS[key] = refusal
        while len(_VALIDATED_GENERATIONS) > _MAX_REMEMBERED_VALIDATIONS:
            _VALIDATED_GENERATIONS.popitem(last=False)


def _already_validated(key: tuple) -> bool:
    """True for bytes proven valid; re-raises the refusal for bytes proven not."""
    verdict = _remembered_verdict(key)
    if verdict is _NOT_REMEMBERED:
        return False
    if verdict is None:
        return True
    raise ValueError(str(verdict))


def _validate_databases_once(
    key: tuple,
    generation_path: Path,
    normalized: dict[str, object],
    graph_schema: str | None,
    state_root: Path,
    *,
    deep: bool,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> None:
    """The expensive half, skipped for bytes already answered either way.

    A semantic refusal is remembered under the same hashed identity as a
    success and re-raised; a deadline or a cancellation is not a verdict on
    the bytes and is not remembered.
    """
    if _already_validated(key):
        return
    try:
        generation_artifact_scan.validate_artifact_databases(
            generation_path,
            normalized,
            graph_schema,
            state_root,
            deep=deep,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
    except ValueError as exc:
        _remember_verdict(key, str(exc))
        raise
    _remember_verdict(key, None)


def validate_generation(
    generation_path: Path,
    state_root: Path,
    *,
    deep: bool = False,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[dict[str, object], tuple[EntrySeal, ...]]:
    check_cancelled(cancelled)
    generation_path = Path(generation_path)
    state_root = Path(state_root)
    stop = {"deadline": deadline, "monotonic": monotonic, "cancelled": cancelled}
    check_monotonic_deadline(deadline, monotonic)
    validate_directory(generation_path, state_root)
    initial_seal, initial_files = scan_generation(generation_path, **stop)
    expected_id = checked_generation_id(generation_path.name)
    check_monotonic_deadline(deadline, monotonic)
    raw = read_runtime_bytes(
        generation_path / "manifest.json", state_root, max_bytes=generation_limits.MAX_MANIFEST_BYTES
    )
    check_monotonic_deadline(deadline, monotonic)
    value = manifest_object(raw)
    normalized, embedding_present, graph_schema = normalized_manifest(
        value, raw, expected_id
    )
    scan = scan_artifacts(value["artifacts"], generation_path, state_root, **stop)
    normalized["artifacts"] = scan.normalized
    normalized["vector_state"] = require_vector_contract(
        value["vector_state"], scan.seen, embedding_present
    )
    require_schema_contract(normalized, graph_schema, scan.seen)
    _validate_databases_once(
        _validation_key(
            generation_path, expected_id, sha256_bytes(raw), scan.digests, deep
        ),
        generation_path,
        normalized,
        graph_schema,
        state_root,
        deep=deep,
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
    )
    final_seal = _require_stable_scan(
        generation_path, initial_seal, initial_files, scan.seen, **stop
    )
    check_monotonic_deadline(deadline, monotonic)
    digests = {"manifest.json": sha256_bytes(raw), **scan.digests}
    return normalized, content_seal(final_seal, digests)


def _recorded_artifact_digest(manifest: dict[str, object], name: str) -> str | None:
    for artifact in manifest.get("artifacts") or []:
        if artifact.get("path") == name:
            return str(artifact.get("sha256"))
    return None


def policy_code_roots(
    generation_path: Path, manifest: dict[str, object], state_root: Path
) -> tuple[str, ...]:
    """The code roots of the snapshot policy, read only when the bytes are the recorded ones.

    A generation built before the manifest named its roots (2026-09-12) still
    says what it was built from in `source-manifest.json`, whose digest the
    manifest binds. Unreadable, or not the recorded bytes: no roots — nobody can
    read such a generation either. See
    `docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md`.
    """
    import evidence_graph

    try:
        raw = read_runtime_bytes(
            generation_path / "source-manifest.json",
            state_root,
            max_bytes=evidence_graph.MAX_SOURCE_MANIFEST_BYTES,
        )
    except (OSError, ValueError):
        return ()
    if sha256_bytes(raw) != _recorded_artifact_digest(manifest, "source-manifest.json"):
        return ()
    return _decoded_policy_roots(raw)


def _decoded_policy_roots(raw: bytes) -> tuple[str, ...]:
    try:
        policy = json.loads(raw).get("policy") or {}
        return tuple(str(name) for name in policy.get("code_roots") or ())
    except (AttributeError, TypeError, ValueError):
        return ()
