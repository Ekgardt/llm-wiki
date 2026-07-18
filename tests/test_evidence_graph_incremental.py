"""Task 23: exact incremental Evidence Graph generation reuse."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source(source_id: str, path: str, content: bytes) -> dict[str, object]:
    return {
        "source_id": source_id,
        "relative_path": path,
        "sha256": _sha(content),
        "size": len(content),
        "media_type": "text/plain",
        "language": "fixture",
        "git_oid": None,
    }


def _snapshot(files: dict[str, tuple[str, bytes]]):
    sources = [_source(source_id, path, content) for source_id, (path, content) in files.items()]
    return sources, {source_id: content for source_id, (_path, content) in files.items()}


def _config(**overrides):
    from evidence_graph_builder import IncrementalReuseConfig

    values = {
        "extractor_version": "fixture-extractor/v1",
        "grammar_version": "fixture-grammar/v1",
        "compiler_version": "fixture-compiler/v1",
        "resolver_config_sha256": _sha(b"resolver"),
        "schema_version": "fixture-schema/v1",
        "workspace_manifest_sha256": _sha(b"workspace"),
    }
    values.update(overrides)
    return IncrementalReuseConfig(**values)


class FixtureExtractor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self,
        source,
        content,
        *,
        sources,
        source_bytes,
        deadline,
        cancelled,
    ):
        from evidence_graph_builder import SourceExtraction

        del sources, deadline
        if cancelled is not None and cancelled():
            raise TimeoutError("fixture extraction cancelled")
        source_id = str(source["source_id"])
        self.calls.append(source_id)
        document = json.loads(content)
        name = str(document["name"])
        node_id = f"node:{name}"
        name_bytes = name.encode()
        name_start = content.index(name_bytes)
        nodes = [
            {
                "node_id": node_id,
                "kind": "fixture",
                "identity_scheme": "fixture/v1",
                "identity_key": name,
                "metadata": {"name": name},
            }
        ]
        occurrences = [
            {
                "occurrence_id": f"occurrence:{source_id}",
                "node_id": node_id,
                "source_id": source_id,
                "role": "definition",
                "byte_start": name_start,
                "byte_end": name_start + len(name_bytes),
                "line_start": 1,
                "line_end": 1,
            }
        ]
        assertions = []
        evidence = []
        dependencies = []
        dependency_sources = tuple(sorted(map(str, document.get("imports", []))))
        for dependency_source in dependency_sources:
            target_document = json.loads(source_bytes[dependency_source])
            target_name = str(target_document["name"])
            target_id = f"node:{target_name}"
            nodes.append(
                {
                    "node_id": target_id,
                    "kind": "fixture",
                    "identity_scheme": "fixture/v1",
                    "identity_key": target_name,
                    "metadata": {"name": target_name},
                }
            )
            assertion_id = f"assertion:{source_id}:{dependency_source}"
            token = dependency_source.encode()
            start = content.index(token)
            assertions.append(
                {
                    "assertion_id": assertion_id,
                    "source_node_id": node_id,
                    "edge_type": "IMPORTS",
                    "target_node_id": target_id,
                    "literal": None,
                    "confidence": "high",
                    "authority": "ai-derived",
                    "resolution": "resolved",
                    "extractor": "fixture-extractor/v1",
                }
            )
            evidence.append(
                {
                    "evidence_id": f"evidence:{source_id}:{dependency_source}",
                    "assertion_id": assertion_id,
                    "observation_id": None,
                    "source_id": source_id,
                    "byte_start": start,
                    "byte_end": start + len(token),
                    "span_sha256": _sha(content[start : start + len(token)]),
                }
            )
            dependencies.append(
                {
                    "dependency_id": f"dependency:{source_id}:{dependency_source}",
                    "dependent_node_id": node_id,
                    "dependency_node_id": target_id,
                    "kind": "imports",
                    "source_id": source_id,
                }
            )
        fingerprints = {
            key: _sha(json.dumps(document.get(key), sort_keys=True).encode())
            for key in ("exports", "imports", "signatures", "aliases", "project_metadata")
        }
        return SourceExtraction(
            nodes=tuple(nodes),
            occurrences=tuple(occurrences),
            assertions=tuple(assertions),
            evidence=tuple(evidence),
            observations=(),
            dependencies=tuple(dependencies),
            source_dependencies=dependency_sources,
            invalidation_fingerprints=fingerprints,
        )


def _build(catalog, generation_id, files, extractor, *, parent=None, config=None, **kwargs):
    from evidence_graph_builder import build_incremental_generation

    sources, source_bytes = _snapshot(files)
    return build_incremental_generation(
        catalog,
        sources=sources,
        source_bytes=source_bytes,
        extractor=extractor,
        reuse_config=config or _config(),
        generation_id=generation_id,
        parent_generation_id=parent,
        expected_active=parent,
        **kwargs,
    )


def _tables(path: Path):
    with sqlite3.connect(path) as database:
        return {
            table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("node", "assertion", "evidence")
        }


def _document(name, *, imports=(), exports=None, signatures=None, aliases=None, project=None):
    return json.dumps(
        {
            "name": name,
            "imports": list(imports),
            "exports": exports if exports is not None else [name],
            "signatures": signatures if signatures is not None else [f"{name}()"],
            "aliases": aliases if aliases is not None else [],
            "project_metadata": project if project is not None else {},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_incremental_detects_snapshot_delta_and_reverse_invalidates_semantic_dependents(tmp_path):
    from generation_catalog import GenerationCatalog

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
    assert second_extractor.calls == ["added", "lib", "renamed-new", "consumer"]
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
    from generation_catalog import GenerationCatalog

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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extractor_version", "fixture-extractor/v2"),
        ("grammar_version", "fixture-grammar/v2"),
        ("compiler_version", "fixture-compiler/v2"),
        ("resolver_config_sha256", _sha(b"resolver-v2")),
        ("schema_version", "fixture-schema/v2"),
        ("workspace_manifest_sha256", _sha(b"workspace-v2")),
    ],
)
def test_reuse_requires_every_exact_config_match(tmp_path, field, value):
    from generation_catalog import GenerationCatalog

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


def test_unchanged_sources_reuse_records_and_every_record_has_dependencies(tmp_path):
    from generation_catalog import GenerationCatalog

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
    from generation_catalog import GenerationCatalog

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


@pytest.mark.parametrize(
    "next_files",
    [
        {"a": ("a.fixture", _document("a")), "b": ("b.fixture", _document("b"))},
        {"a": ("a.fixture", _document("a", aliases=("alias",))), "b": ("b.fixture", _document("b", imports=("a",)))},
        {"b": ("renamed.fixture", _document("b"))},
    ],
)
def test_incremental_canonical_graph_equals_clean_full_rebuild(tmp_path, next_files):
    from generation_catalog import GenerationCatalog

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
    from generation_catalog import GenerationCatalog

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
    from generation_catalog import GenerationCatalog

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
    from generation_catalog import GenerationCatalog

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
