"""Task 23: exact incremental Evidence Graph generation reuse."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.evidence_graph_incremental_helpers import (
    FixtureExtractor,
    _build,
    _config,
    _document,
    _sha,
    _snapshot,
    _tables,
)

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_incremental_detects_snapshot_delta_and_reverse_invalidates_semantic_dependents(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    original = {
        "lib": ("lib.fixture", _document("lib")),
        "consumer": ("consumer.fixture", _document("consumer", imports=("lib",))),
        "deleted": ("deleted.fixture", _document("deleted")),
        "renamed-old": ("old.fixture", _document("renamed")),
    }
    first_extractor = FixtureExtractor()
    first = _build(catalog, "gen-1", original, first_extractor)
    assert first.rebuilt_sources == ("consumer", "deleted", "lib", "renamed-old")

    changed = {
        "lib": ("lib.fixture", _document("lib", signatures=("lib(value)",))),
        "consumer": original["consumer"],
        "added": ("added.fixture", _document("added")),
        "renamed-new": ("new.fixture", original["renamed-old"][1]),
    }
    second_extractor = FixtureExtractor()
    second = _build(catalog, "gen-2", changed, second_extractor, parent="gen-1")

    assert second.added_sources == ("added", "renamed-new")
    assert second.changed_sources == ("lib",)
    assert second.deleted_sources == ("deleted", "renamed-old")
    assert second.renamed_sources == (("renamed-old", "renamed-new"),)
    assert second.rebuilt_sources == ("added", "consumer", "lib", "renamed-new")
    assert second.reused_sources == ()
    assert second_extractor.calls == ["added", "consumer", "lib", "renamed-new"]
    assert second.activated
    manifest = json.loads((second.generation_path / "incremental-manifest.json").read_bytes())
    assert {record["status"] for record in manifest["record_dependencies"]} == {"rebuilt"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exports", ("renamed",)),
        ("imports", ("consumer",)),
        ("signatures", ("lib(value)",)),
        ("aliases", ("public-lib",)),
        ("project", {"root": "packages/lib"}),
    ],
)
def test_every_semantic_fingerprint_invalidates_reverse_dependents(tmp_path, field, value):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    original = {
        "lib": ("lib.fixture", _document("lib")),
        "consumer": ("consumer.fixture", _document("consumer", imports=("lib",))),
    }
    _build(catalog, "gen-1", original, FixtureExtractor())
    options = {field: value}
    changed = {**original, "lib": ("lib.fixture", _document("lib", **options))}
    extractor = FixtureExtractor()

    result = _build(catalog, "gen-2", changed, extractor, parent="gen-1")

    assert result.rebuilt_sources == ("consumer", "lib")
    assert extractor.calls == ["lib", "consumer"]


def test_content_only_edit_keeps_unrelated_source_reuse_targeted(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    original = {
        "lib": ("lib.fixture", _document("lib")),
        "consumer": ("consumer.fixture", _document("consumer", imports=("lib",))),
        "ordinary": ("ordinary.fixture", _document("ordinary")),
    }
    _build(catalog, "gen-1", original, FixtureExtractor())
    changed = {
        **original,
        "lib": ("lib.fixture", _document("lib", signatures=("lib(value)",))),
    }
    extractor = FixtureExtractor()

    result = _build(catalog, "gen-2", changed, extractor, parent="gen-1")

    assert result.rebuilt_sources == ("consumer", "lib")
    assert result.reused_sources == ("ordinary",)
    assert extractor.calls == ["lib", "consumer"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extractor_version", "fixture-extractor/v2"),
        ("grammar_version", "fixture-grammar/v2"),
        ("compiler_version", "fixture-compiler/v2"),
        ("resolver_config_sha256", _sha(b"resolver-v2")),
        ("schema_version", "fixture-schema/v2"),
    ],
)
def test_reuse_requires_every_exact_config_match(tmp_path, field, value):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(catalog, "gen-1", files, FixtureExtractor())
    extractor = FixtureExtractor()

    result = _build(
        catalog,
        "gen-2",
        files,
        extractor,
        parent="gen-1",
        config=_config(**{field: value}),
    )

    assert result.reused_sources == ()
    assert result.rebuilt_sources == ("one",)
    assert extractor.calls == ["one"]


def test_workspace_manifest_change_rebuilds_all_current_workspace_sources(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {
        "ordinary": ("ordinary.fixture", _document("ordinary")),
        "watcher": ("watcher.fixture", _document("watcher")),
    }
    _build(
        catalog,
        "gen-1",
        files,
        FixtureExtractor(workspace_sensitive=("watcher",)),
    )
    extractor = FixtureExtractor(workspace_sensitive=("watcher",))

    result = _build(
        catalog,
        "gen-2",
        files,
        extractor,
        parent="gen-1",
        config=_config(workspace_manifest_sha256=_sha(b"workspace-v2")),
    )

    assert result.rebuilt_sources == ("ordinary", "watcher")
    assert result.reused_sources == ()
    assert extractor.calls == ["ordinary", "watcher"]


def test_deleted_workspace_source_rebuilds_all_surviving_workspace_sources(
    tmp_path,
):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {
        "deleted": ("deleted.fixture", _document("deleted")),
        "survivor": ("survivor.fixture", _document("survivor")),
        "ordinary": ("ordinary.fixture", _document("ordinary")),
    }
    _build(
        catalog,
        "gen-1",
        files,
        FixtureExtractor(workspace_sensitive=("deleted", "survivor")),
    )
    extractor = FixtureExtractor(workspace_sensitive=("survivor",))

    result = _build(
        catalog,
        "gen-2",
        {key: value for key, value in files.items() if key != "deleted"},
        extractor,
        parent="gen-1",
    )

    assert result.rebuilt_sources == ("ordinary", "survivor")
    assert result.reused_sources == ()
    assert extractor.calls == ["ordinary", "survivor"]


def test_reuse_requires_exact_parent_repository_scope(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog
    from repository_scope import resolve_repository_scope

    first_root = tmp_path / "first" / "same-name"
    second_root = tmp_path / "second" / "same-name"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first_scope = resolve_repository_scope(first_root)
    second_scope = resolve_repository_scope(second_root)
    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(
        catalog,
        "gen-1",
        files,
        FixtureExtractor(),
        repository_scope=first_scope,
    )
    extractor = FixtureExtractor()

    result = _build(
        catalog,
        "gen-2",
        files,
        extractor,
        parent="gen-1",
        repository_scope=second_scope,
    )

    assert first_scope.repository_id != second_scope.repository_id
    assert result.reused_sources == ()
    assert result.rebuilt_sources == ("one",)
    assert extractor.calls == ["one"]
    assert result.manifest["repository_scope"] == second_scope.as_dict()


def test_unchanged_sources_reuse_records_and_every_record_has_dependencies(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {
        "lib": ("lib.fixture", _document("lib")),
        "consumer": ("consumer.fixture", _document("consumer", imports=("lib",))),
    }
    _build(catalog, "gen-1", files, FixtureExtractor())
    extractor = FixtureExtractor()

    result = _build(catalog, "gen-2", files, extractor, parent="gen-1")

    assert result.reused_sources == ("consumer", "lib")
    assert result.rebuilt_sources == ()
    assert extractor.calls == []
    manifest = json.loads((result.generation_path / "incremental-manifest.json").read_bytes())
    records = manifest["record_dependencies"]
    assert records
    assert {record["status"] for record in records} == {"reused"}
    assert all(record["source_ids"] for record in records)


def test_delete_removes_only_records_exclusive_to_deleted_source(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {
        "left": ("left.fixture", _document("shared")),
        "right": ("right.fixture", _document("shared")),
    }
    _build(catalog, "gen-1", files, FixtureExtractor())
    result = _build(
        catalog,
        "gen-2",
        {"right": files["right"]},
        FixtureExtractor(),
        parent="gen-1",
    )

    with sqlite3.connect(result.generation_path / "evidence.sqlite3") as database:
        assert database.execute("SELECT node_id FROM node").fetchall() == [("node:shared",)]
        assert database.execute("SELECT source_id FROM occurrence").fetchall() == [("right",)]


def test_language_change_is_a_source_change_even_when_bytes_and_path_match(tmp_path):
    from evidence_graph_builder import build_incremental_generation
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(catalog, "gen-1", files, FixtureExtractor())
    sources, source_bytes = _snapshot(files)
    sources[0]["language"] = "other-fixture"
    extractor = FixtureExtractor()

    result = build_incremental_generation(
        catalog,
        sources=sources,
        source_bytes=source_bytes,
        extractor=extractor,
        reuse_config=_config(),
        generation_id="gen-2",
        parent_generation_id="gen-1",
        expected_active="gen-1",
    )

    assert result.changed_sources == ("one",)
    assert result.rebuilt_sources == ("one",)
    assert extractor.calls == ["one"]


def test_record_ownership_is_inverted_once_not_rescanned_per_source():
    import evidence_graph_builder

    class CountingOwnership(dict):
        def __init__(self, values):
            super().__init__(values)
            self.item_iterations = 0

        def items(self):
            self.item_iterations += 1
            return super().items()

    ownership = CountingOwnership(
        {
            ("nodes", f"node:{index}"): {f"source:{index % 20}"}
            for index in range(200)
        }
    )
    source_ids = tuple(f"source:{index}" for index in range(20))

    def legacy_ids(records, collection, source_id):
        return sorted(
            record_id
            for (record_collection, record_id), owners in records.items()
            if record_collection == collection
            if source_id in owners
        )

    def legacy_group(records, owners):
        return {
            source_id: {
                collection: legacy_ids(records, collection, source_id)
                for collection in evidence_graph_builder.RECORD_COLLECTIONS
            }
            for source_id in owners
        }

    group = getattr(evidence_graph_builder, "record_ids_by_owner", legacy_group)
    grouped = group(ownership, source_ids)

    assert ownership.item_iterations == 1
    assert sum(len(records["nodes"]) for records in grouped.values()) == 200


def test_record_ownership_inversion_checks_cancellation_during_grouping():
    import evidence_graph_builder

    class CountingOwnership(dict):
        def __init__(self, values):
            super().__init__(values)
            self.yielded = 0

        def items(self):
            for item in super().items():
                self.yielded += 1
                yield item

    ownership = CountingOwnership(
        {
            ("nodes", f"node:{index}"): {"source:one"}
            for index in range(20)
        }
    )

    with pytest.raises(TimeoutError, match="cancel"):
        evidence_graph_builder.record_ids_by_owner(
            ownership,
            ("source:one",),
            cancelled=lambda: ownership.yielded >= 3,
        )

    assert ownership.yielded == 3


def test_workspace_sensitive_manifest_is_linear_at_10k_sources():
    import evidence_graph_builder
    from reliable_memory import canonical_json_bytes

    source_count = 10_000
    fingerprints = {
        key: "0" * 64
        for key in ("exports", "imports", "signatures", "aliases", "project_metadata")
    }
    entries = [
        {
            "source_id": f"source:{index:05d}",
            "relative_path": f"src/{index:05d}.py",
            "sha256": "0" * 64,
            "source_dependencies": [],
            "workspace_sensitive": index % 2 == 0,
            "invalidation_fingerprints": fingerprints,
            "records": {
                collection: []
                for collection in evidence_graph_builder.RECORD_COLLECTIONS
            },
            "language": "python",
        }
        for index in range(source_count)
    ]
    manifest = {
        "version": evidence_graph_builder.INCREMENTAL_MANIFEST_VERSION,
        "reuse_config": {
            "extractor_version": "fixture/v1",
            "grammar_version": "fixture/v1",
            "compiler_version": "fixture/v1",
            "resolver_config_sha256": "0" * 64,
            "schema_version": "fixture/v1",
            "workspace_manifest_sha256": "0" * 64,
        },
        "sources": entries,
        "record_dependencies": [],
    }

    evidence_graph_builder.validated_incremental_manifest(manifest)
    encoded = canonical_json_bytes(manifest)

    class CountingEntries(dict):
        def __init__(self, values):
            super().__init__(values)
            self.yielded = 0

        def items(self):
            for item in super().items():
                self.yielded += 1
                yield item

    by_id = CountingEntries({entry["source_id"]: entry for entry in entries})
    sensitive = evidence_graph_builder.workspace_sensitive_source_ids(by_id)

    assert len(encoded) < source_count * 1_000
    assert encoded.count(b'"workspace_sensitive":') == source_count
    assert len(sensitive) == source_count // 2
    assert by_id.yielded == source_count


@pytest.mark.parametrize(
    "next_files",
    [
        {"a": ("a.fixture", _document("a")), "b": ("b.fixture", _document("b"))},
        {"a": ("a.fixture", _document("a", aliases=("alias",))), "b": ("b.fixture", _document("b", imports=("a",)))},
        {"b": ("renamed.fixture", _document("b"))},
    ],
)
def test_incremental_canonical_graph_equals_clean_full_rebuild(tmp_path, next_files):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    base = {
        "a": ("a.fixture", _document("a")),
        "b": ("b.fixture", _document("b", imports=("a",))),
    }
    incremental_catalog = GenerationCatalog(tmp_path / "incremental")
    _build(incremental_catalog, "base", base, FixtureExtractor())
    incremental = _build(
        incremental_catalog,
        "next",
        next_files,
        FixtureExtractor(),
        parent="base",
    )
    clean_catalog = GenerationCatalog(tmp_path / "clean")
    clean = _build(clean_catalog, "clean", next_files, FixtureExtractor())

    assert _tables(incremental.generation_path / "evidence.sqlite3") == _tables(
        clean.generation_path / "evidence.sqlite3"
    )


def test_incremental_cancellation_does_not_publish_partial_generation(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(catalog, "gen-1", files, FixtureExtractor())

    with pytest.raises(TimeoutError, match="cancel"):
        _build(
            catalog,
            "gen-2",
            {"one": ("one.fixture", _document("one", exports=("changed",)))},
            FixtureExtractor(),
            parent="gen-1",
            cancelled=lambda: True,
        )

    assert catalog.get_active()["generation_id"] == "gen-1"
    assert not (catalog.generations_path / "gen-2").exists()


def test_incremental_task19_kill_point_preserves_prior_active_generation(tmp_path):
    from evidence_graph_builder import KillPointError
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(catalog, "gen-1", files, FixtureExtractor())

    with pytest.raises(KillPointError, match="before_activation"):
        _build(
            catalog,
            "gen-2",
            files,
            FixtureExtractor(),
            parent="gen-1",
            kill_point="before_activation",
        )

    assert catalog.get_active()["generation_id"] == "gen-1"
    assert (catalog.generations_path / "gen-2" / "incremental-manifest.json").exists()


def test_incremental_extractor_cannot_mutate_the_pinned_source_snapshot(tmp_path):
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    delegate = FixtureExtractor()

    def mutating_extractor(source, content, **kwargs):
        with pytest.raises(TypeError):
            source["relative_path"] = "changed.fixture"
        with pytest.raises(TypeError):
            kwargs["sources"][0]["sha256"] = "0" * 64
        with pytest.raises(TypeError):
            kwargs["source_bytes"]["one"] = b"changed"
        return delegate(source, content, **kwargs)

    result = _build(catalog, "gen-1", files, mutating_extractor)

    with sqlite3.connect(result.generation_path / "evidence.sqlite3") as database:
        assert database.execute("SELECT relative_path, sha256 FROM source").fetchone() == (
            "one.fixture",
            _sha(files["one"][1]),
        )

def test_a_manifest_too_large_to_store_still_builds_the_generation(tmp_path, monkeypatch):
    """The manifest only buys the next pass its reuse; the vault still needs a build."""
    import evidence_graph_builder
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {
        "lib": ("lib.fixture", _document("lib")),
        "consumer": ("consumer.fixture", _document("consumer", imports=("lib",))),
    }
    _build(catalog, "gen-1", files, FixtureExtractor())
    changed = {**files, "lib": ("lib.fixture", _document("lib", signatures=("lib(v)",)))}
    monkeypatch.setattr(
        evidence_graph_builder, "MAX_STORED_INCREMENTAL_MANIFEST_BYTES", 1
    )

    result = _build(catalog, "gen-2", changed, FixtureExtractor(), parent="gen-1")

    assert result.generation_id == "gen-2"
    assert set(result.rebuilt_sources) == {"consumer", "lib"}


def test_a_parent_the_builder_cannot_read_is_rebuilt_in_full(tmp_path, monkeypatch):
    """Reuse is an optimisation; a parent whose manifest fails validation means no reuse."""
    import evidence_graph_builder
    from llm_wiki.generation_catalog_store import GenerationCatalog

    catalog = GenerationCatalog(tmp_path / "state")
    files = {"one": ("one.fixture", _document("one"))}
    _build(catalog, "gen-1", files, FixtureExtractor())

    def refuse(*args, **kwargs):
        raise ValueError("incremental manifest has an unsupported version")

    monkeypatch.setattr(evidence_graph_builder, "load_incremental_manifest", refuse)
    extractor = FixtureExtractor()
    result = _build(catalog, "gen-2", files, extractor, parent="gen-1")

    assert (result.reused_sources, result.rebuilt_sources, extractor.calls) == ((), ("one",), ["one"])
