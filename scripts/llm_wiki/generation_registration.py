"""Manifests a registration names and the scope it publishes under."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from llm_wiki import generation_descriptors
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from repository_scope import RepositoryScope


def _decoded_manifest(encoded: bytes) -> dict[str, object] | None:
    """The manifest object these bytes canonically encode, or None."""
    try:
        manifest = json.loads(encoded)
    except (TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(manifest, dict) or canonical_json_bytes(manifest) != encoded:
        return None
    return manifest


def verified_manifest(row: sqlite3.Row) -> dict[str, object] | None:
    """A registration row's manifest when its bytes still prove themselves."""
    encoded = bytes(row["manifest_json"])
    if sha256_bytes(encoded) != row["manifest_sha256"]:
        return None
    return _decoded_manifest(encoded)


def manifest_belongs_to(
    manifest: dict[str, object], repository_scope: RepositoryScope
) -> bool:
    """Whether this manifest's own scope names the same repository checkout.

    Identity only: a generation built at another commit of the same checkout
    still belongs to it. See `RepositoryScope.identity` and NEW-65.
    """
    try:
        registered = RepositoryScope.from_dict(manifest.get("repository_scope"))
    except (TypeError, ValueError):
        return False
    return registered.same_repository(repository_scope)


def record_scanned_entry(seal, path: Path, relative: str, pending: list, files: set) -> None:
    if seal.kind == "directory":
        pending.append(path)
        return
    files.add(relative)


def close_descriptors(held_files: list) -> None:
    for held in held_files:
        try:
            os.close(held.descriptor)
        except OSError:
            pass


def require_registration_match(row, encoded: bytes, digest: str) -> None:
    if row is None:
        return
    if bytes(row["manifest_json"]) != encoded or row["manifest_sha256"] != digest:
        raise ValueError("generation registration is immutable")


def require_registration_token(registered, token: tuple[bytes, str]) -> None:
    if registered is None:
        raise ValueError("generation registration disappeared")
    stored = (bytes(registered["manifest_json"]), registered["manifest_sha256"])
    if stored != token:
        raise ValueError("generation registration changed after validation")


def require_fallback_registration(registered, token: tuple[bytes, str] | None) -> None:
    if registered is None:
        raise ValueError("fallback registration changed after validation")
    stored = (bytes(registered["manifest_json"]), registered["manifest_sha256"])
    if stored != token:
        raise ValueError("fallback registration changed after validation")


def selected_registration_token(
    selected: dict[str, object] | None, selected_id: str | None
) -> tuple[bytes, str] | None:
    if selected_id is None or selected is None:
        return None
    encoded = canonical_json_bytes(selected)
    return encoded, sha256_bytes(encoded)


def unlink_manifest(generation_path: Path) -> None:
    """Remove the one file that makes a tree a publication; a linked directory is left alone."""
    try:
        linked = generation_descriptors.is_link_or_reparse(generation_path)
    except FileNotFoundError:
        return
    if not linked:
        (generation_path / "manifest.json").unlink(missing_ok=True)


def still_referenced(database: sqlite3.Connection, identifier: str) -> bool:
    referenced = database.execute(
        "SELECT 1 FROM generations WHERE generation_id = ? "
        "UNION ALL "
        "SELECT 1 FROM catalog_state "
        "WHERE singleton = 1 AND active_generation_id = ? "
        "UNION ALL "
        "SELECT 1 FROM activation_history WHERE generation_id = ? LIMIT 1",
        (identifier, identifier, identifier),
    ).fetchone()
    return referenced is not None


def candidate_manifest(candidate) -> dict[str, object]:
    """The manifest the candidate carries, still canonical and still its own."""
    if sha256_bytes(candidate.manifest_bytes) != candidate.manifest_sha256:
        raise ValueError("validated candidate manifest hash changed")
    try:
        manifest = json.loads(candidate.manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("validated candidate manifest is invalid") from exc
    _require_canonical_candidate(manifest, candidate)
    scope = manifest_scope(manifest)
    scope_bytes = None if scope is None else canonical_json_bytes(scope.as_dict())
    if scope_bytes != candidate.repository_scope_bytes:
        raise ValueError("validated candidate repository scope changed")
    return manifest


def _canonical_candidate(manifest: object, candidate) -> bool:
    """Order matters: the byte comparison is only meaningful for a dict."""
    return (
        isinstance(manifest, dict)
        and canonical_json_bytes(manifest) == candidate.manifest_bytes
        and manifest.get("generation_id") == candidate.generation_id
    )


def _require_canonical_candidate(manifest: object, candidate) -> None:
    if not _canonical_candidate(manifest, candidate):
        raise ValueError("validated candidate manifest is not canonical")


def manifest_scope(manifest: dict[str, object]) -> RepositoryScope | None:
    scope_value = manifest.get("repository_scope")
    if scope_value is None:
        return None
    return RepositoryScope.from_dict(scope_value)


def require_publication_scope(
    scope: RepositoryScope | None, expected: RepositoryScope | None
) -> None:
    """Publication still binds the exact scope, commit included: it records what
    the generation was built from. Eligibility later asks a weaker question."""
    if expected is None:
        return
    if scope != RepositoryScope.from_dict(expected.as_dict()):
        raise ValueError("generation repository scope does not match publication scope")
