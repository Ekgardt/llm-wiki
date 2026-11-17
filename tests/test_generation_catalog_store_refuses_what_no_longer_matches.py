from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.generation_catalog_store import GenerationCatalog
from repository_scope import RepositoryScope, resolve_repository_scope

from tests.generation_catalog_helpers import NOW, _publish


def _catalog(tmp_path: Path, monotonic: Callable[[], float] | None = None) -> GenerationCatalog:
    state_root = tmp_path / "state"
    state_root.mkdir(exist_ok=True)
    return GenerationCatalog(state_root, clock=lambda: NOW, monotonic=monotonic)


def _catalog_database(catalog: GenerationCatalog) -> sqlite3.Connection:
    return sqlite3.connect(catalog.catalog_path)


def _rewrite_manifest(directory: Path, field: str, value: object) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest[field] = value
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def _tamper(artifact: Path) -> None:
    artifact.chmod(0o600)
    artifact.write_bytes(b"tampered-index")


def _as_fields(scope: RepositoryScope) -> dict[str, object]:
    return dict(scope.as_dict())


def _scope(tmp_path: Path) -> RepositoryScope:
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    return resolve_repository_scope(checkout)


def test_an_unregistered_generation_cannot_be_activated(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "g1")
    with pytest.raises(ValueError, match="generation is not registered"):
        catalog.activate("g1", expected_active=None)


def test_a_generation_whose_manifest_was_rewritten_cannot_be_activated(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "g1")
    catalog.register("g1")
    _rewrite_manifest(directory, "collector_version", "collector/v2")
    with pytest.raises(ValueError, match="no longer matches its manifest"):
        catalog.activate("g1", expected_active=None)


def test_a_candidate_scope_must_be_a_repository_scope(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "g1")
    with pytest.raises(TypeError, match="RepositoryScope or None"):
        catalog._validate_candidate("g1", expected_repository_scope=object())  # type: ignore[arg-type]


def test_a_repository_question_needs_a_repository_scope(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    with pytest.raises(TypeError, match="repository_scope must be a RepositoryScope"):
        catalog.get_active_for_repository("checkout")  # type: ignore[arg-type]


def test_discarding_an_unknown_generation_removes_nothing(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "g1")
    assert catalog.discard_unactivated("g2") is False
    assert (directory / "manifest.json").exists()


def test_a_catalog_without_its_active_pointer_row_is_refused(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    with _catalog_database(catalog) as database:
        database.execute("DELETE FROM catalog_state")
    with pytest.raises(ValueError, match="catalog active pointer is missing"):
        catalog.get_active()


def test_an_active_generation_with_a_damaged_artifact_falls_back_to_its_parent(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "g1")
    catalog.register("g1")
    assert catalog.activate("g1", expected_active=None)
    second, _manifest = _publish(catalog, "g2", parent="g1", payload=b"second-index")
    catalog.register("g2")
    assert catalog.activate("g2", expected_active="g1")
    _tamper(second / "search.sqlite3")
    active = catalog.get_active()
    assert active is not None
    assert active["generation_id"] == "g1"


def test_a_registration_whose_hash_no_longer_proves_it_has_no_repository(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    scope = _scope(tmp_path)
    _publish(catalog, "g1", repository_scope=_as_fields(scope))
    catalog.register("g1")
    assert catalog._registered_repository_scope("g1") == scope
    with _catalog_database(catalog) as database:
        database.execute("UPDATE generations SET manifest_sha256 = ?", ("0" * 64,))
    assert catalog._registered_repository_scope("g1") is None
    assert catalog._registered_repository_scope("g9") is None


def test_a_parent_of_another_checkout_is_not_a_fallback_for_this_one(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    scope = _scope(tmp_path)
    foreign = resolve_repository_scope(tmp_path / "state")
    _publish(catalog, "g1", repository_scope=_as_fields(foreign))
    catalog.register("g1")
    assert catalog.activate("g1", expected_active=None)
    second, _manifest = _publish(
        catalog, "g2", parent="g1", payload=b"second-index", repository_scope=_as_fields(scope)
    )
    catalog.register("g2")
    assert catalog.activate("g2", expected_active="g1")
    _tamper(second / "search.sqlite3")
    assert catalog.get_active_for_repository(scope) is None
    unscoped = catalog.get_active()
    assert unscoped is not None
    assert unscoped["generation_id"] == "g1"


def test_a_damaged_unactivated_generation_of_this_checkout_is_not_offered(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    scope = _scope(tmp_path)
    directory, _manifest = _publish(catalog, "g1", repository_scope=_as_fields(scope))
    catalog.register("g1")
    offered = catalog.get_active_for_repository(scope)
    assert offered is not None
    assert offered["generation_id"] == "g1"
    _tamper(directory / "search.sqlite3")
    assert catalog.get_active_for_repository(scope) is None


class _ClockThatRunsOut:
    def __init__(self, readings_before_expiry: int) -> None:
        self.readings_left = readings_before_expiry

    def __call__(self) -> float:
        self.readings_left -= 1
        return float(self.readings_left < 0) * 100.0


def test_a_deadline_that_passes_while_the_catalog_opens_stops_the_read(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path, monotonic=_ClockThatRunsOut(2))
    with pytest.raises(TimeoutError, match="generation catalog deadline reached"):
        catalog.get_active(deadline=50.0)
