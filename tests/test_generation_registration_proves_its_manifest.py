from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from llm_wiki import generation_registration
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from repository_scope import RepositoryScope, resolve_repository_scope


def _registration_row(encoded: bytes, digest: str) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    row: sqlite3.Row = database.execute(
        "SELECT ? AS manifest_json, ? AS manifest_sha256", (encoded, digest)
    ).fetchone()
    database.close()
    return row


def _proved_row(encoded: bytes) -> sqlite3.Row:
    return _registration_row(encoded, sha256_bytes(encoded))


def test_a_row_whose_bytes_prove_a_canonical_object_yields_that_manifest() -> None:
    manifest = {"generation_id": "g1", "schema_version": "v2"}
    row = _proved_row(canonical_json_bytes(manifest))
    assert generation_registration.verified_manifest(row) == manifest


def test_a_row_whose_hash_no_longer_matches_yields_nothing() -> None:
    row = _registration_row(canonical_json_bytes({"generation_id": "g1"}), "0" * 64)
    assert generation_registration.verified_manifest(row) is None


@pytest.mark.parametrize(
    "encoded",
    [b"{not json", b"\xff\xfe", b"[1,2]", b'{ "generation_id": "g1" }'],
)
def test_a_row_that_is_not_a_canonical_object_yields_nothing(encoded: bytes) -> None:
    assert generation_registration.verified_manifest(_proved_row(encoded)) is None


def _scope_of(directory: Path) -> RepositoryScope:
    directory.mkdir(parents=True, exist_ok=True)
    return resolve_repository_scope(directory)


def test_a_manifest_belongs_to_the_checkout_its_scope_names(tmp_path: Path) -> None:
    scope = _scope_of(tmp_path / "checkout")
    manifest: dict[str, object] = {"repository_scope": scope.as_dict()}
    other = _scope_of(tmp_path / "other")
    assert generation_registration.manifest_belongs_to(manifest, scope)
    assert not generation_registration.manifest_belongs_to(manifest, other)


@pytest.mark.parametrize("recorded", [None, {"repository_id": "r"}, "scope"])
def test_a_manifest_without_a_valid_scope_belongs_to_no_checkout(
    tmp_path: Path, recorded: object
) -> None:
    scope = _scope_of(tmp_path / "checkout")
    manifest: dict[str, object] = {"repository_scope": recorded}
    assert not generation_registration.manifest_belongs_to(manifest, scope)


def test_a_scanned_directory_is_queued_and_a_file_is_recorded(tmp_path: Path) -> None:
    pending: list[Path] = []
    files: set[str] = set()
    directory_seal = SimpleNamespace(kind="directory")
    file_seal = SimpleNamespace(kind="file")
    generation_registration.record_scanned_entry(
        directory_seal, tmp_path / "sub", "sub", pending, files
    )
    generation_registration.record_scanned_entry(
        file_seal, tmp_path / "a.db", "a.db", pending, files
    )
    assert pending == [tmp_path / "sub"]
    assert files == {"a.db"}


def test_closing_held_descriptors_survives_one_already_closed(tmp_path: Path) -> None:
    target = tmp_path / "held.bin"
    target.write_bytes(b"x")
    already_closed = os.open(target, os.O_RDONLY)
    os.close(already_closed)
    still_open = os.open(target, os.O_RDONLY)
    held = [SimpleNamespace(descriptor=already_closed), SimpleNamespace(descriptor=still_open)]
    generation_registration.close_descriptors(held)
    with pytest.raises(OSError):
        os.fstat(still_open)


def test_a_registration_that_differs_from_the_stored_one_is_immutable() -> None:
    encoded = canonical_json_bytes({"generation_id": "g1"})
    stored = _proved_row(encoded)
    generation_registration.require_registration_match(None, encoded, sha256_bytes(encoded))
    generation_registration.require_registration_match(stored, encoded, sha256_bytes(encoded))
    changed = canonical_json_bytes({"generation_id": "g2"})
    with pytest.raises(ValueError, match="generation registration is immutable"):
        generation_registration.require_registration_match(stored, changed, sha256_bytes(changed))


def test_a_registration_token_must_still_name_the_stored_registration() -> None:
    encoded = canonical_json_bytes({"generation_id": "g1"})
    stored = _proved_row(encoded)
    generation_registration.require_registration_token(stored, (encoded, sha256_bytes(encoded)))
    with pytest.raises(ValueError, match="generation registration disappeared"):
        generation_registration.require_registration_token(None, (encoded, "x"))
    with pytest.raises(ValueError, match="changed after validation"):
        generation_registration.require_registration_token(stored, (encoded, "x"))


def test_a_fallback_registration_must_still_match_its_token() -> None:
    encoded = canonical_json_bytes({"generation_id": "g1"})
    stored = _proved_row(encoded)
    token = generation_registration.selected_registration_token({"generation_id": "g1"}, "g1")
    generation_registration.require_fallback_registration(stored, token)
    with pytest.raises(ValueError, match="fallback registration changed"):
        generation_registration.require_fallback_registration(None, token)
    with pytest.raises(ValueError, match="fallback registration changed"):
        generation_registration.require_fallback_registration(stored, None)


def test_no_selection_has_no_registration_token() -> None:
    assert generation_registration.selected_registration_token(None, "g1") is None
    assert generation_registration.selected_registration_token({"a": 1}, None) is None


def test_unlinking_a_missing_generation_is_a_no_op(tmp_path: Path) -> None:
    generation_registration.unlink_manifest(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_unlinking_removes_the_manifest_of_a_real_directory(tmp_path: Path) -> None:
    generation = tmp_path / "g1"
    generation.mkdir()
    (generation / "manifest.json").write_bytes(b"{}")
    generation_registration.unlink_manifest(generation)
    assert not (generation / "manifest.json").exists()


def test_unlinking_leaves_a_linked_generation_alone(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "manifest.json").write_bytes(b"{}")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    generation_registration.unlink_manifest(linked)
    assert (real / "manifest.json").exists()


def _candidate(manifest: dict[str, object], scope: RepositoryScope | None) -> SimpleNamespace:
    encoded = canonical_json_bytes(manifest)
    return SimpleNamespace(
        manifest_bytes=encoded,
        manifest_sha256=sha256_bytes(encoded),
        generation_id=manifest.get("generation_id"),
        repository_scope_bytes=_scope_bytes(scope),
    )


def _scope_bytes(scope: RepositoryScope | None) -> bytes | None:
    if scope is None:
        return None
    return canonical_json_bytes(scope.as_dict())


def test_a_candidate_manifest_is_returned_when_it_is_still_its_own(tmp_path: Path) -> None:
    scope = _scope_of(tmp_path / "checkout")
    manifest: dict[str, object] = {"generation_id": "g1", "repository_scope": scope.as_dict()}
    assert generation_registration.candidate_manifest(_candidate(manifest, scope)) == manifest


def test_a_candidate_whose_hash_changed_is_refused() -> None:
    candidate = _candidate({"generation_id": "g1"}, None)
    candidate.manifest_sha256 = "0" * 64
    with pytest.raises(ValueError, match="manifest hash changed"):
        generation_registration.candidate_manifest(candidate)


def test_a_candidate_whose_bytes_are_not_json_is_refused() -> None:
    candidate = _candidate({"generation_id": "g1"}, None)
    candidate.manifest_bytes = b"{broken"
    candidate.manifest_sha256 = sha256_bytes(b"{broken")
    with pytest.raises(ValueError, match="candidate manifest is invalid"):
        generation_registration.candidate_manifest(candidate)


def test_a_candidate_naming_another_generation_is_not_canonical() -> None:
    candidate = _candidate({"generation_id": "g1"}, None)
    candidate.generation_id = "g2"
    with pytest.raises(ValueError, match="manifest is not canonical"):
        generation_registration.candidate_manifest(candidate)


def test_a_candidate_whose_scope_changed_is_refused(tmp_path: Path) -> None:
    scope = _scope_of(tmp_path / "checkout")
    manifest: dict[str, object] = {"generation_id": "g1"}
    with pytest.raises(ValueError, match="repository scope changed"):
        generation_registration.candidate_manifest(_candidate(manifest, scope))


def test_publication_binds_the_exact_scope_it_was_handed(tmp_path: Path) -> None:
    scope = _scope_of(tmp_path / "checkout")
    other = _scope_of(tmp_path / "other")
    generation_registration.require_publication_scope(None, None)
    generation_registration.require_publication_scope(scope, scope)
    with pytest.raises(ValueError, match="does not match publication scope"):
        generation_registration.require_publication_scope(other, scope)


def _reference_database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.execute("CREATE TABLE generations (generation_id TEXT)")
    database.execute("CREATE TABLE catalog_state (singleton INTEGER, active_generation_id TEXT)")
    database.execute("CREATE TABLE activation_history (generation_id TEXT)")
    database.execute("INSERT INTO catalog_state VALUES (1, 'active')")
    database.execute("INSERT INTO activation_history VALUES ('earlier')")
    database.execute("INSERT INTO generations VALUES ('registered')")
    return database


@pytest.mark.parametrize(
    ("identifier", "referenced"),
    [("active", True), ("earlier", True), ("registered", True), ("unknown", False)],
)
def test_a_generation_is_referenced_by_any_catalog_record(
    identifier: str, referenced: bool
) -> None:
    database = _reference_database()
    try:
        assert generation_registration.still_referenced(database, identifier) is referenced
    finally:
        database.close()
