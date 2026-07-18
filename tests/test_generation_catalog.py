"""Immutable derived-generation catalog contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


class _Monotonic:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def _rewrite_preserving_metadata(path: Path, content: bytes) -> None:
    before = path.stat(follow_symlinks=False)
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _catalog(tmp_path: Path):
    import generation_catalog

    state_root = tmp_path / "state"
    return generation_catalog.GenerationCatalog(state_root, clock=lambda: NOW)


def _publish(
    catalog,
    generation_id: str,
    *,
    parent: str | None = None,
    payload: bytes = b"search-index",
    vector_state: str = "absent",
    extra_artifacts: int = 0,
) -> tuple[Path, dict[str, object]]:
    from reliable_memory import canonical_json_bytes

    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    artifact = directory / "search.sqlite3"
    artifact.write_bytes(payload)
    manifest: dict[str, object] = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v1",
        "collector_version": "collector/v1",
        "extractor_version": "extractor/v1",
        "tokenizer_version": "tokenizer/v1",
        "tokenizer_config_sha256": hashlib.sha256(b"tokenizer-config").hexdigest(),
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": None,
        "graph_extractor_version": None,
        "source_manifest_sha256": hashlib.sha256(b"sources").hexdigest(),
        "artifacts": [
            {
                "path": "search.sqlite3",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
        "vector_state": vector_state,
    }
    if parent is not None:
        manifest["parent_generation_id"] = parent
    for number in range(extra_artifacts):
        name = f"artifact-{number:04d}.bin"
        content = f"artifact-{number:04d}".encode()
        (directory / name).write_bytes(content)
        manifest["artifacts"].append(
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest["artifacts"].sort(key=lambda artifact: artifact["path"])
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return directory, manifest


def test_catalog_uses_required_layout_pragmas_and_generic_tables(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")

    catalog.register("gen-1")

    assert catalog.catalog_path == (tmp_path / "state/cache/evidence-graph/catalog.sqlite3")
    assert catalog.generations_path == (tmp_path / "state/cache/evidence-graph/generations")
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        tables = {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert database.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert tables == {"generations", "catalog_state", "activation_history", "sqlite_sequence"}
    assert not catalog.catalog_path.with_name("catalog.sqlite3-wal").exists()


def test_registration_is_immutable_idempotent_and_does_not_mutate_generation(tmp_path):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    assert catalog.register("gen-1") == manifest
    assert catalog.register("gen-1") == manifest
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before

    changed = dict(manifest)
    changed["collector_version"] = "collector/v2"
    (directory / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable|canonical"):
        catalog.register("gen-1")


def test_registration_rechecks_manifest_digest_inside_transaction(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    manifest_path = directory / "manifest.json"
    changed = manifest_path.read_bytes().replace(b"collector/v1", b"collector/v2")
    real_check = catalog._seal_unchanged

    def mutate_then_check(generation_path, seal):
        _rewrite_preserving_metadata(manifest_path, changed)
        return real_check(generation_path, seal)

    monkeypatch.setattr(catalog, "_seal_unchanged", mutate_then_check)

    with pytest.raises(ValueError, match="changed|seal"):
        catalog.register("gen-1")
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update(extra=True),
        lambda manifest: manifest.update(generation_id="other"),
        lambda manifest: manifest.update(vector_state="partial"),
        lambda manifest: manifest.update(source_manifest_sha256="ABC"),
        lambda manifest: manifest.pop("tokenizer_config_sha256"),
        lambda manifest: manifest["artifacts"][0].update(extra=True),
        lambda manifest: manifest["artifacts"][0].update(path="../escape"),
        lambda manifest: manifest["artifacts"][0].update(size=True),
    ],
)
def test_manifest_contract_fails_closed(tmp_path, mutate):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1")
    mutate(manifest)
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises((TypeError, ValueError, PermissionError)):
        catalog.register("gen-1")


def test_manifest_binds_closed_future_retrieval_metadata(tmp_path):
    catalog = _catalog(tmp_path)
    _directory, manifest = _publish(catalog, "gen-1")

    registered = catalog.register("gen-1")

    assert registered == manifest
    assert set(registered) == {
        "generation_id",
        "schema_version",
        "collector_version",
        "extractor_version",
        "tokenizer_version",
        "tokenizer_config_sha256",
        "embedding_model_id",
        "embedding_model_revision",
        "vector_dimensions",
        "graph_schema_version",
        "graph_extractor_version",
        "source_manifest_sha256",
        "artifacts",
        "vector_state",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tokenizer_version", None),
        ("tokenizer_config_sha256", "config-name"),
        ("embedding_model_id", "model"),
        ("embedding_model_revision", "revision"),
        ("vector_dimensions", 384),
        ("graph_schema_version", "graph/v1"),
        ("graph_extractor_version", "graph-extractor/v1"),
    ],
)
def test_manifest_rejects_incomplete_or_inconsistent_future_metadata(tmp_path, field, value):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1")
    manifest[field] = value
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises((TypeError, ValueError)):
        catalog.register("gen-1")


def test_manifest_accepts_bound_vector_and_graph_metadata(tmp_path):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1", vector_state="complete")
    for name, content in (("vectors.json", b"{}"), ("vectors.npy", b"numpy")):
        (directory / name).write_bytes(content)
        manifest["artifacts"].append(
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest["artifacts"].sort(key=lambda artifact: artifact["path"])
    manifest.update(
        embedding_model_id="model/name",
        embedding_model_revision="commit-123",
        vector_dimensions=384,
        graph_schema_version="graph/v1",
        graph_extractor_version="graph-extractor/v1",
    )
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    assert catalog.register("gen-1") == manifest


@pytest.mark.parametrize("damage", ["missing", "size", "hash"])
def test_registration_rejects_missing_wrong_size_or_wrong_hash_artifact(tmp_path, damage):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    artifact = directory / "search.sqlite3"
    if damage == "missing":
        artifact.unlink()
    elif damage == "size":
        artifact.write_bytes(b"x")
    else:
        data = artifact.read_bytes()
        artifact.write_bytes(b"X" + data[1:])

    with pytest.raises((ValueError, PermissionError)):
        catalog.register("gen-1")


def test_complete_vectors_reject_missing_declared_vector_artifact(tmp_path):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1", vector_state="complete")
    manifest["artifacts"].append({"path": "vectors.npy", "size": 10, "sha256": "0" * 64})
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises((ValueError, PermissionError)):
        catalog.register("gen-1")


def test_compare_and_swap_has_one_winner_and_rejects_stale_expected_active(tmp_path):
    catalog = _catalog(tmp_path)
    for generation_id in ("gen-1", "gen-2"):
        _publish(catalog, generation_id)
        catalog.register(generation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda generation_id: catalog.activate(generation_id, expected_active=None),
                ("gen-1", "gen-2"),
            )
        )

    assert sorted(results) == [False, True]
    winner = catalog.get_active()
    assert winner is not None
    assert winner["generation_id"] in {"gen-1", "gen-2"}
    loser = ({"gen-1", "gen-2"} - {winner["generation_id"]}).pop()
    assert catalog.activate(loser, expected_active=None) is False


def test_activation_rechecks_validation_seal_inside_cas_transaction(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    artifact = directory / "search.sqlite3"

    real_check = catalog._seal_unchanged

    def mutate_and_check(generation_path, seal):
        _rewrite_preserving_metadata(artifact, b"SEARCH-INDEX")
        return real_check(generation_path, seal)

    monkeypatch.setattr(catalog, "_seal_unchanged", mutate_and_check, raising=False)

    with pytest.raises(ValueError, match="changed|seal"):
        catalog.activate("gen-1", expected_active=None)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        history = database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0]
    assert active is None
    assert history == 0


def test_get_active_falls_back_and_repairs_pointer_after_active_corruption(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    catalog.register("gen-1")
    catalog.register("gen-2")
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    (catalog.generations_path / "gen-2/search.sqlite3").write_bytes(b"corrupt")

    active = catalog.get_active()

    assert active is not None
    assert active["generation_id"] == "gen-1"
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        pointer = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    assert pointer == "gen-1"


def test_fallback_rechecks_selected_generation_seal_before_pointer_repair(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    fallback, _manifest = _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    catalog.register("gen-1")
    catalog.register("gen-2")
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    (catalog.generations_path / "gen-2/search.sqlite3").write_bytes(b"corrupt")
    fallback_artifact = fallback / "search.sqlite3"

    real_check = catalog._seal_unchanged

    def mutate_and_check(generation_path, seal):
        _rewrite_preserving_metadata(fallback_artifact, b"SEARCH-INDEX")
        return real_check(generation_path, seal)

    monkeypatch.setattr(catalog, "_seal_unchanged", mutate_and_check)

    with pytest.raises(ValueError, match="changed|seal"):
        catalog.get_active()
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        pointer = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        history = database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0]
    assert pointer == "gen-2"
    assert history == 2


def test_get_active_propagates_catalog_errors_without_demoting_pointer(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)

    def fail_validation(_generation_id):
        raise sqlite3.OperationalError("catalog temporarily unavailable")

    monkeypatch.setattr(catalog, "_registered_generation", fail_validation)

    with pytest.raises(sqlite3.OperationalError, match="temporarily unavailable"):
        catalog.get_active()
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        pointer = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    assert pointer == "gen-1"


def test_fallback_prefers_prior_activation_before_an_older_parent(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    _publish(catalog, "gen-3", parent="gen-1")
    for generation_id in ("gen-1", "gen-2", "gen-3"):
        catalog.register(generation_id)
    assert catalog.activate("gen-2", expected_active=None)
    assert catalog.activate("gen-3", expected_active="gen-2")
    (catalog.generations_path / "gen-3/search.sqlite3").write_bytes(b"corrupt")

    active = catalog.get_active()

    assert active is not None
    assert active["generation_id"] == "gen-2"


def test_recover_registers_only_complete_valid_immediate_orphans_without_activation(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "good")
    bad, _manifest = _publish(catalog, "bad")
    (bad / "search.sqlite3").unlink()
    incomplete = catalog.generations_path / "incomplete"
    incomplete.mkdir(parents=True)
    (incomplete / "partial.tmp").write_bytes(b"partial")

    assert catalog.recover_orphans() == ["good"]
    assert catalog.recover_orphans() == []
    assert catalog.get_active() is None
    assert bad.exists() and incomplete.exists()


def test_recovery_propagates_catalog_operational_failures(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    _publish(catalog, "orphan")

    def fail_registration(_generation_id):
        raise sqlite3.OperationalError("catalog unavailable")

    monkeypatch.setattr(catalog, "register", fail_registration)

    with pytest.raises(sqlite3.OperationalError, match="catalog unavailable"):
        catalog.recover_orphans()


def test_catalog_explicitly_closes_every_opened_connection(tmp_path, monkeypatch):
    import generation_catalog

    opened = []
    real_write = generation_catalog.open_operational_db
    real_read = generation_catalog.open_readonly_operational_db

    class TrackingConnection:
        def __init__(self, database):
            self.database = database
            self.closed = False

        def __getattr__(self, name):
            return getattr(self.database, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.database.__exit__(*args)

        def close(self):
            self.closed = True
            self.database.close()

    def tracked_write(*args, **kwargs):
        connection = TrackingConnection(real_write(*args, **kwargs))
        opened.append(connection)
        return connection

    def tracked_read(*args, **kwargs):
        connection = TrackingConnection(real_read(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(generation_catalog, "open_operational_db", tracked_write)
    monkeypatch.setattr(generation_catalog, "open_readonly_operational_db", tracked_read)
    try:
        catalog = _catalog(tmp_path)
        _publish(catalog, "gen-1")
        catalog.register("gen-1")
        assert catalog.activate("gen-1", expected_active=None)
        assert catalog.get_active() is not None

        assert opened
        assert all(connection.closed for connection in opened)
    finally:
        for connection in opened:
            if not connection.closed:
                connection.close()


def test_catalog_row_ceilings_prevent_generation_and_history_growth(tmp_path, monkeypatch):
    import generation_catalog

    monkeypatch.setattr(generation_catalog, "MAX_GENERATIONS", 2, raising=False)
    monkeypatch.setattr(generation_catalog, "MAX_ACTIVATION_HISTORY", 1, raising=False)
    catalog = _catalog(tmp_path)
    for generation_id in ("gen-1", "gen-2", "gen-3"):
        _publish(catalog, generation_id)
    catalog.register("gen-1")
    catalog.register("gen-2")

    with pytest.raises(ValueError, match="generation.*ceiling"):
        catalog.register("gen-3")
    assert catalog.activate("gen-1", expected_active=None)
    with pytest.raises(ValueError, match="history.*ceiling"):
        catalog.activate("gen-2", expected_active="gen-1")

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        counts = (
            database.execute("SELECT COUNT(*) FROM generations").fetchone()[0],
            database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0],
        )
        pointer = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    assert counts == (2, 1)
    assert pointer == "gen-1"


def test_catalog_byte_ceiling_rolls_back_large_generation_and_remains_reopenable(tmp_path):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        page_count = database.execute("PRAGMA main.page_count").fetchone()[0]
        page_size = database.execute("PRAGMA main.page_size").fetchone()[0]
    byte_cap = page_count * page_size + page_size
    limited = generation_catalog.GenerationCatalog(
        catalog.state_root,
        max_catalog_bytes=byte_cap,
        clock=lambda: NOW,
    )
    _publish(limited, "gen-2", extra_artifacts=300)

    with pytest.raises(ValueError, match="catalog.*byte ceiling"):
        limited.register("gen-2")

    reopened = generation_catalog.GenerationCatalog(
        catalog.state_root,
        max_catalog_bytes=byte_cap,
        clock=lambda: NOW,
    )
    assert reopened.get_active()["generation_id"] == "gen-1"
    with closing(sqlite3.connect(reopened.catalog_path)) as database:
        generation_ids = {
            row[0] for row in database.execute("SELECT generation_id FROM generations")
        }
        actual_bytes = (
            database.execute("PRAGMA main.page_count").fetchone()[0]
            * database.execute("PRAGMA main.page_size").fetchone()[0]
        )
    assert generation_ids == {"gen-1"}
    assert actual_bytes <= byte_cap


def test_catalog_byte_ceiling_rolls_back_history_append_and_preserves_active(tmp_path):
    import generation_catalog

    catalog = _catalog(tmp_path)
    for generation_id in ("gen-1", "gen-2"):
        _publish(catalog, generation_id)
        catalog.register(generation_id)
    assert catalog.activate("gen-1", expected_active=None)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        page_count = database.execute("PRAGMA main.page_count").fetchone()[0]
        page_size = database.execute("PRAGMA main.page_size").fetchone()[0]
    byte_cap = page_count * page_size + page_size - 1
    limited = generation_catalog.GenerationCatalog(
        catalog.state_root,
        max_catalog_bytes=byte_cap,
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="catalog.*byte ceiling"):
        limited.activate("gen-2", expected_active="gen-1")

    reopened = generation_catalog.GenerationCatalog(
        catalog.state_root,
        max_catalog_bytes=byte_cap,
        clock=lambda: NOW,
    )
    assert reopened.get_active()["generation_id"] == "gen-1"
    with closing(sqlite3.connect(reopened.catalog_path)) as database:
        history = database.execute(
            "SELECT generation_id FROM activation_history ORDER BY sequence"
        ).fetchall()
    assert history == [("gen-1",)]


def test_deadline_during_streamed_hash_leaves_registration_unchanged(tmp_path, monkeypatch):
    import generation_catalog

    monotonic = _Monotonic()
    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state",
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    payload = b"x" * (generation_catalog.HASH_CHUNK_BYTES * 3)
    _publish(catalog, "gen-1", payload=payload)
    real_read = generation_catalog.os.read

    def expire_after_artifact_read(descriptor, size):
        data = real_read(descriptor, size)
        if os.fstat(descriptor).st_size == len(payload) and data:
            monotonic.value = 2.0
        return data

    monkeypatch.setattr(generation_catalog.os, "read", expire_after_artifact_read)

    with pytest.raises(TimeoutError, match="deadline"):
        catalog.register("gen-1", deadline=1.0)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0


def test_deadline_bounds_writer_lock_admission_and_leaves_registration_unchanged(
    tmp_path, monkeypatch
):
    import generation_catalog

    monotonic = _Monotonic(10.0)
    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state",
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    _publish(catalog, "gen-1")
    blocker = sqlite3.connect(catalog.catalog_path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    real_open = generation_catalog.open_operational_db
    busy_values: list[int] = []

    def tracked_open(path, *, busy_ms):
        busy_values.append(busy_ms)
        return real_open(path, busy_ms=busy_ms)

    monkeypatch.setattr(generation_catalog, "open_operational_db", tracked_open)
    try:
        with pytest.raises(TimeoutError, match="deadline|writer"):
            catalog.register("gen-1", deadline=10.01)
    finally:
        blocker.rollback()
        blocker.close()
    assert busy_values and 0 <= busy_values[-1] <= 10
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0


def test_deadline_recomputes_busy_timeout_immediately_before_commit(tmp_path, monkeypatch):
    import generation_catalog

    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state", clock=lambda: NOW, monotonic=_Monotonic()
    )
    _publish(catalog, "gen-1")
    real_open = generation_catalog.open_operational_db
    remaining_values = iter((900, 800, 400))
    observed: dict[str, int] = {}

    class TrackedConnection:
        def __init__(self, database):
            self.database = database

        def __getattr__(self, name):
            return getattr(self.database, name)

        def commit(self):
            observed["commit_busy_ms"] = self.database.execute("PRAGMA busy_timeout").fetchone()[0]
            self.database.commit()

        def close(self):
            self.database.close()

    def tracked_open(path, *, busy_ms):
        return TrackedConnection(real_open(path, busy_ms=busy_ms))

    monkeypatch.setattr(generation_catalog, "open_operational_db", tracked_open)
    monkeypatch.setattr(catalog, "_remaining_busy_ms", lambda _deadline: next(remaining_values))

    catalog.register("gen-1", deadline=1.0)

    assert observed == {"commit_busy_ms": 400}


def test_deadline_immediately_before_cas_rolls_back_pointer_and_history(tmp_path, monkeypatch):
    import generation_catalog

    monotonic = _Monotonic()
    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state",
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    for generation_id in ("gen-1", "gen-2"):
        _publish(catalog, generation_id)
        catalog.register(generation_id)
    assert catalog.activate("gen-1", expected_active=None)
    real_check = catalog._seal_unchanged

    def expire_after_seal(generation_path, seal, **kwargs):
        valid = real_check(generation_path, seal, **kwargs)
        monotonic.value = 2.0
        return valid

    monkeypatch.setattr(catalog, "_seal_unchanged", expire_after_seal)

    with pytest.raises(TimeoutError, match="deadline"):
        catalog.activate("gen-2", expected_active="gen-1", deadline=1.0)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        pointer = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        history = database.execute(
            "SELECT generation_id FROM activation_history ORDER BY sequence"
        ).fetchall()
    assert pointer == "gen-1"
    assert history == [("gen-1",)]


def test_recovery_returns_committed_prefix_when_deadline_expires(tmp_path, monkeypatch):
    import generation_catalog

    monotonic = _Monotonic()
    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state",
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    for generation_id in ("orphan-a", "orphan-b"):
        _publish(catalog, generation_id)
    real_register = catalog.register

    def expire_after_first_commit(generation_id, *, deadline=None):
        result = real_register(generation_id, deadline=deadline)
        monotonic.value = 2.0
        return result

    monkeypatch.setattr(catalog, "register", expire_after_first_commit)

    assert catalog.recover_orphans(deadline=1.0) == ["orphan-a"]
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        registered = database.execute(
            "SELECT generation_id FROM generations ORDER BY generation_id"
        ).fetchall()
    assert registered == [("orphan-a",)]


def test_public_operations_reject_non_finite_deadlines(tmp_path):
    catalog = _catalog(tmp_path)
    operations = (
        lambda: catalog.register("missing", deadline=float("inf")),
        lambda: catalog.activate("missing", expected_active=None, deadline=float("inf")),
        lambda: catalog.get_active(deadline=float("inf")),
        lambda: catalog.recover_orphans(deadline=float("inf")),
    )

    for operation in operations:
        with pytest.raises(ValueError, match="deadline"):
            operation()


def test_symlink_artifact_and_generation_path_escape_are_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1")
    outside = tmp_path / "outside"
    outside.write_bytes(b"search-index")
    artifact = directory / "search.sqlite3"
    artifact.unlink()
    try:
        artifact.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises((ValueError, PermissionError)):
        catalog.register("gen-1")
    assert outside.read_bytes() == b"search-index"
    with pytest.raises(ValueError):
        catalog.register("../gen-1")
    assert manifest["generation_id"] == "gen-1"


def test_reparse_artifact_is_rejected_without_mutation(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    artifact = directory / "search.sqlite3"
    before = artifact.read_bytes()
    real_check = generation_catalog._is_link_or_reparse
    monkeypatch.setattr(
        generation_catalog,
        "_is_link_or_reparse",
        lambda path: path.name == "search.sqlite3" or real_check(path),
    )

    with pytest.raises(PermissionError, match="reparse"):
        catalog.register("gen-1")
    assert artifact.read_bytes() == before


def test_manifest_and_artifact_bounds_are_enforced(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1")
    monkeypatch.setattr(generation_catalog, "MAX_ARTIFACTS", 0)
    with pytest.raises(ValueError, match="artifact"):
        catalog.register("gen-1")

    monkeypatch.setattr(generation_catalog, "MAX_ARTIFACTS", 1024)
    monkeypatch.setattr(generation_catalog, "MAX_MANIFEST_BYTES", 4)
    assert os.path.getsize(directory / "manifest.json") > 4
    with pytest.raises((ValueError, PermissionError), match="manifest|bounded"):
        catalog.register("gen-1")


def test_artifact_hashing_uses_bounded_descriptor_reads(tmp_path, monkeypatch):
    import generation_catalog

    chunk_size = 64 * 1024
    payload = b"x" * (chunk_size * 3 + 17)
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1", payload=payload)
    real_read = generation_catalog.os.read
    artifact_reads: list[int] = []

    def recording_read(descriptor, size):
        if os.fstat(descriptor).st_size == len(payload):
            artifact_reads.append(size)
            assert size <= chunk_size
        return real_read(descriptor, size)

    monkeypatch.setattr(generation_catalog.os, "read", recording_read)

    catalog.register("gen-1")

    assert len(artifact_reads) >= 4
    assert max(artifact_reads) <= chunk_size


def test_artifact_directory_scan_stops_before_sorting_oversized_input(tmp_path, monkeypatch):
    import generation_catalog

    directory = tmp_path / "generation"
    directory.mkdir()
    for number in range(30):
        (directory / f"artifact-{number:02d}").write_bytes(b"")
    real_scandir = generation_catalog.os.scandir
    yielded = 0

    class CountingScandir:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            self._entries.__enter__()
            return self

        def __exit__(self, *args):
            return self._entries.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            entry = next(self._entries)
            yielded += 1
            return entry

    monkeypatch.setattr(generation_catalog, "MAX_ARTIFACTS", 1)
    monkeypatch.setattr(generation_catalog.os, "scandir", CountingScandir)

    with pytest.raises(ValueError, match="too many"):
        generation_catalog._listed_generation_files(directory)
    assert yielded == 21


def test_orphan_scan_stops_before_sorting_oversized_input(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    for number in range(10):
        (catalog.generations_path / f"generation-{number}").mkdir()
    real_scandir = generation_catalog.os.scandir
    yielded = 0

    class CountingScandir:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            self._entries.__enter__()
            return self

        def __exit__(self, *args):
            return self._entries.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            entry = next(self._entries)
            yielded += 1
            return entry

    monkeypatch.setattr(generation_catalog, "MAX_GENERATION_CHILDREN", 2)
    monkeypatch.setattr(generation_catalog.os, "scandir", CountingScandir)

    with pytest.raises(ValueError, match="child count"):
        catalog.recover_orphans()
    assert yielded == 3
