"""Immutable derived-generation catalog contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
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


def _write_preserving_times(path: Path, content: bytes, before) -> None:
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _require_before_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise AssertionError("filesystem did not expose the test mutation")


def _rewrite_preserving_metadata(path: Path, content: bytes) -> None:
    before = path.stat(follow_symlinks=False)
    assert len(content) == before.st_size
    _write_preserving_times(path, content, before)
    if os.name != "posix":
        return
    deadline = time.monotonic() + 1
    while path.stat(follow_symlinks=False).st_ctime_ns == before.st_ctime_ns:
        _require_before_deadline(deadline)
        time.sleep(0.001)
        _write_preserving_times(path, content, before)


def _apply_artifact_mutation(mutation: str, target: Path, changed: bytes, replacement: Path) -> None:
    """Change the bytes while keeping every stat field a reader could compare."""
    if mutation == "in-place":
        _rewrite_preserving_metadata(target, changed)
        return
    os.replace(replacement, target)


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
    repository_scope: dict[str, object] | None = None,
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
    if repository_scope is not None:
        manifest["repository_scope"] = repository_scope
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


def _publish_v2(catalog, generation_id: str) -> tuple[Path, dict[str, object]]:
    import corpus_snapshot
    import evidence_graph
    import search_memory
    from reliable_memory import canonical_json_bytes

    vault = catalog.state_root.parent / f"vault-{generation_id}"
    (vault / "knowledge/notes").mkdir(parents=True)
    (vault / "knowledge/projects").mkdir(parents=True)
    (vault / "knowledge/notes/page.md").write_text(
        "---\n"
        "type: concept\n"
        "project: generation-tests\n"
        "source_authority: user\n"
        "confidence: high\n"
        "status: active\n"
        "valid_from: 2026-01-01\n"
        "valid_to: 2027-01-01\n"
        "language: en\n"
        "---\n"
        "# Bound source\nunique generation content\n",
        encoding="utf-8",
    )
    snapshot = corpus_snapshot.collect_corpus(vault)
    directory = catalog.generations_path / generation_id
    directory.mkdir(parents=True)
    source_manifest = corpus_snapshot.canonical_source_manifest(
        (source.record for source in snapshot.sources), snapshot.policy
    )
    (directory / "source-manifest.json").write_bytes(canonical_json_bytes(source_manifest))
    evidence_graph.create_generation_database(
        directory / "evidence.sqlite3",
        sources=(
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
        ),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
    )
    search_memory.build_generation_fts(snapshot, directory)

    artifacts = []
    for name in ("evidence.sqlite3", "search.sqlite3", "source-manifest.json"):
        payload = (directory / name).read_bytes()
        artifacts.append(
            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        )
    manifest = {
        "generation_id": generation_id,
        "schema_version": "corpus-generation/v2",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": None,
        "embedding_model_revision": None,
        "vector_dimensions": None,
        "graph_schema_version": evidence_graph.GRAPH_SCHEMA_VERSION,
        "graph_extractor_version": "fixture-graph/v1",
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": sorted(artifacts, key=lambda item: item["path"]),
        "vector_state": "absent",
        "repository_scope": __import__("repository_scope").resolve_repository_scope(
            vault
        ).as_dict(),
    }
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return directory, manifest


@pytest.mark.parametrize(
    ("failure", "closed_handles", "closed_descriptors"),
    [
        ("before-transfer", [123], []),
        ("after-transfer", [], [41]),
        (None, [], []),
    ],
)
@pytest.mark.skipif(os.name != "nt", reason="requires Windows handle conversion")
def test_windows_read_descriptor_closes_exact_owner_on_conversion_failure(
    tmp_path, monkeypatch, failure, closed_handles, closed_descriptors
):
    import generation_catalog

    path = tmp_path / "artifact.bin"
    path.write_bytes(b"artifact")
    actual_handles = []
    actual_descriptors = []

    monkeypatch.setattr(generation_catalog, "_create_file", lambda *_args: 123)
    monkeypatch.setattr(
        generation_catalog, "_close_handle", lambda handle: actual_handles.append(handle)
    )

    def convert(_handle, _flags):
        if failure == "before-transfer":
            raise OSError("conversion failed")
        return 41

    def set_inheritable(_descriptor, _inheritable):
        if failure == "after-transfer":
            raise OSError("inheritability failed")

    monkeypatch.setattr(generation_catalog.msvcrt, "open_osfhandle", convert)
    monkeypatch.setattr(generation_catalog.os, "set_inheritable", set_inheritable)
    monkeypatch.setattr(
        generation_catalog.os,
        "close",
        lambda descriptor: actual_descriptors.append(descriptor),
    )

    if failure is None:
        assert generation_catalog._open_read_descriptor(path) == 41
    else:
        with pytest.raises(OSError, match="failed"):
            generation_catalog._open_read_descriptor(path)

    assert actual_handles == closed_handles
    assert actual_descriptors == closed_descriptors


def _refresh_artifact(directory: Path, manifest: dict[str, object], name: str) -> None:
    from reliable_memory import canonical_json_bytes

    payload = (directory / name).read_bytes()
    descriptor = next(item for item in manifest["artifacts"] if item["path"] == name)
    descriptor.update(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def test_v2_requires_complete_exact_artifact_set(tmp_path):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-missing-search")
    (directory / "search.sqlite3").unlink()
    manifest["artifacts"] = [
        item for item in manifest["artifacts"] if item["path"] != "search.sqlite3"
    ]
    from reliable_memory import canonical_json_bytes

    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="v2.*artifact|artifact.*v2"):
        catalog.register("v2-missing-search")

    extra_directory, extra_manifest = _publish_v2(catalog, "v2-extra")
    payload = b"undeclared-contract-extension"
    (extra_directory / "extra.bin").write_bytes(payload)
    extra_manifest["artifacts"].append(
        {"path": "extra.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )
    extra_manifest["artifacts"].sort(key=lambda item: item["path"])
    (extra_directory / "manifest.json").write_bytes(canonical_json_bytes(extra_manifest))

    with pytest.raises(ValueError, match="v2.*artifact|artifact.*v2"):
        catalog.register("v2-extra")


def test_v2_rejects_semantically_invalid_search_after_hash_recomputed(tmp_path):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-invalid-search")
    with sqlite3.connect(directory / "search.sqlite3") as database:
        database.execute(
            "UPDATE generation_metadata SET value='wrong-tokenizer' "
            "WHERE key='tokenizer_version'"
        )
        database.commit()
    _refresh_artifact(directory, manifest, "search.sqlite3")

    with pytest.raises(ValueError, match="FTS|search"):
        catalog.register("v2-invalid-search")


def test_v2_complete_generation_registers(tmp_path):
    catalog = _catalog(tmp_path)
    _directory, manifest = _publish_v2(catalog, "v2-complete")

    assert catalog.register("v2-complete") == manifest


def test_registered_non_code_v2_fixture_activates_without_capture(
    non_code_v2_generation,
) -> None:
    from generation_catalog import GenerationCatalog

    assert "code_capture" not in non_code_v2_generation.manifest
    assert GenerationCatalog(
        non_code_v2_generation.generation_path.parents[3]
    ).get_active()["generation_id"] == non_code_v2_generation.generation_id


def test_v1_search_only_generation_accepts_null_graph_schema(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _directory, manifest = _publish(catalog, "v1-search-only")

    assert manifest["graph_schema_version"] is None
    assert catalog.register("v1-search-only") == manifest


@pytest.mark.parametrize("damage", ["source_id", "relative_path", "sha256"])
def test_catalog_binds_v1_graph_v2_capture_to_sources(tmp_path: Path, damage: str) -> None:
    import stat

    from code_workspace import code_capture_as_dict
    from corpus_snapshot import (
        CodeCaptureContract,
        CodeCaptureFile,
        FileStatMetadata,
        RepositoryCodeLimits,
        RepositoryCodePolicy,
    )
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, f"v1-capture-{damage}")
    manifest["schema_version"] = "corpus-generation/v1"
    source_manifest = json.loads((directory / "source-manifest.json").read_bytes())
    source = source_manifest["sources"][0]
    with closing(sqlite3.connect(directory / "evidence.sqlite3")) as database:
        size = database.execute(
            "SELECT size FROM source WHERE source_id=?", (source["logical_id"],)
        ).fetchone()[0]
    capture_file = CodeCaptureFile(
        source["logical_id"],
        source["relative_path"],
        source["sha256"],
        FileStatMetadata(size, 0, 0, stat.S_IFREG, 1, 1),
    )
    contract = CodeCaptureContract(
        RepositoryCodePolicy(
            (source["relative_path"],), ("**",), (), (Path(source["relative_path"]).suffix,)
        ),
        RepositoryCodeLimits(),
        (capture_file,),
        (),
        "0" * 64,
    )
    capture = code_capture_as_dict(contract)
    if damage == "source_id":
        capture["files"][0]["source_id"] = "source:other"
    elif damage == "relative_path":
        capture["files"][0]["relative_path"] = "other.md"
        capture["policy"]["roots"] = ["other.md"]
    else:
        capture["files"][0]["sha256"] = "f" * 64
    capture["membership_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()
    manifest["code_capture"] = capture
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="source membership"):
        catalog.register(directory.name)


def test_v1_generation_rejects_graph_v3(tmp_path: Path) -> None:
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "v1-v3")
    manifest["graph_schema_version"] = "evidence-graph/v3"
    manifest["graph_extractor_version"] = "graph/v3"
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="v3|corpus-generation/v2"):
        catalog.register("v1-v3")


@pytest.mark.parametrize("graph_schema", ["evidence-graph/v2", "evidence-graph/v3"])
def test_v2_generation_accepts_only_known_graph_schema_strings(
    tmp_path: Path, graph_schema: str
) -> None:
    from code_intelligence import verify_native_analysis
    from corpus_snapshot import collect_corpus
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    from tests.code_kernel_helpers import (
        captured_snapshot_for_records,
        make_analysis_scope,
        make_normalized_analysis,
    )

    catalog = _catalog(tmp_path)
    if graph_schema == "evidence-graph/v3":
        repository = tmp_path / "repository"
        (repository / "knowledge/notes").mkdir(parents=True)
        (repository / "knowledge/projects").mkdir(parents=True)
        (repository / "knowledge/notes/page.md").write_text(
            "---\ntype: concept\n---\n# Page\nCanonical content.\n",
            encoding="utf-8",
        )
        snapshot = collect_corpus(repository)
        records = {
            "sources": [
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
            ],
            "source_bytes": {
                source.record.logical_id: source.content for source in snapshot.sources
            },
            "nodes": (),
            "occurrences": (),
            "assertions": (),
            "evidence": (),
            "observations": (),
            "dependencies": (),
        }
        snapshot = __import__("dataclasses").replace(
            snapshot,
            code_capture=captured_snapshot_for_records(records).code_capture,
        )
        scope = make_analysis_scope(snapshot)
        repository_scope = resolve_repository_scope(repository)
        result = build_full_generation(
            catalog,
            generation_id="known-v3",
            graph_schema=GraphSchema.V3,
            verified_analyses=(
                verify_native_analysis(
                    snapshot,
                    make_normalized_analysis(snapshot, scope, repository_scope),
                ),
            ),
            snapshot=snapshot,
            code_capture=snapshot.code_capture,
            repository_scope=repository_scope,
            activate=False,
            **records,
        )
        assert result.manifest["schema_version"] == "corpus-generation/v2"
        assert result.manifest["graph_schema_version"] == graph_schema
        assert catalog.register("known-v3") == result.manifest
        return
    directory, manifest = _publish_v2(catalog, "known-v2")
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    assert catalog.register(directory.name)["graph_schema_version"] == graph_schema


@pytest.mark.parametrize("graph_schema", [None, "unknown-graph/v1"])
def test_v2_generation_rejects_null_and_unknown_graph_schema(
    tmp_path: Path, graph_schema: str | None
) -> None:
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "invalid-v2-graph")
    manifest["graph_schema_version"] = graph_schema
    if graph_schema is None:
        manifest["graph_extractor_version"] = None
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="graph schema|Evidence Graph"):
        catalog.register("invalid-v2-graph")


def test_catalog_rejects_v3_database_with_v2_manifest(tmp_path: Path) -> None:
    from reliable_memory import canonical_json_bytes

    from tests.code_kernel_helpers import publish_v3_fixture

    result = publish_v3_fixture(tmp_path, generation_id="mismatch")
    manifest_path = result.generation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["graph_schema_version"] = "evidence-graph/v2"
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="schema|contract|version"):
        _catalog(tmp_path).register("mismatch")


_V3_CAPTURE_DAMAGE = {
    "missing": lambda manifest, capture: manifest.pop("code_capture"),
    "unknown-top": lambda manifest, capture: capture.update(unknown=True),
    "unknown-nested": lambda manifest, capture: capture["limits"].update(unknown=1),
    "membership-hash": lambda manifest, capture: capture.update(
        membership_sha256="0" * 64
    ),
    "directory-order": lambda manifest, capture: capture.update(
        directories=list(reversed(capture["directories"]))
    ),
    "source-id": lambda manifest, capture: capture["files"][0].update(
        source_id="source:wrong.py"
    ),
    "source-hash": lambda manifest, capture: capture["files"][0].update(
        sha256="f" * 64
    ),
    "policy-nfc": lambda manifest, capture: capture["policy"].update(
        include_globs=["cafe\u0301/**"]
    ),
    "stat-mtime": lambda manifest, capture: capture["files"][0]["stat"].update(
        mtime_ns=capture["files"][0]["stat"]["mtime_ns"] + 1
    ),
}


def _write_damaged_manifest(manifest_path: Path, manifest: dict, damage: str) -> None:
    """Only the NFC case must bypass canonical encoding — that is its point."""
    from reliable_memory import canonical_json_bytes

    if damage != "policy-nfc":
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        return
    manifest_path.write_bytes(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )


@pytest.mark.parametrize("damage", sorted(_V3_CAPTURE_DAMAGE))
def test_catalog_rejects_damaged_or_noncanonical_v3_code_capture(
    tmp_path: Path, damage: str
) -> None:
    from tests.code_kernel_helpers import publish_v3_fixture

    result = publish_v3_fixture(tmp_path, generation_id=f"capture-{damage}")
    manifest_path = result.generation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    _V3_CAPTURE_DAMAGE[damage](manifest, manifest.get("code_capture"))
    _write_damaged_manifest(manifest_path, manifest, damage)

    with pytest.raises(
        ValueError,
        match="code.capture|v3|manifest|membership|order|source_id|source membership",
    ):
        _catalog(tmp_path).register(result.generation_id)


@pytest.mark.parametrize("damage", ["identity", "hash"])
def test_catalog_matches_capture_identity_and_hash_to_source_manifest(
    tmp_path: Path, damage: str
) -> None:
    from reliable_memory import canonical_json_bytes

    from tests.code_kernel_helpers import publish_v3_fixture

    result = publish_v3_fixture(tmp_path, generation_id=f"source-{damage}")
    manifest_path = result.generation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    capture = manifest["code_capture"]
    file = capture["files"][0]
    if damage == "hash":
        file["sha256"] = "f" * 64
    else:
        old_path = file["relative_path"]
        file["source_id"] = "source:other.py"
        file["relative_path"] = "other.py"
        capture["policy"]["roots"] = sorted(
            "other.py" if root == old_path else root
            for root in capture["policy"]["roots"]
        )
    capture["membership_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="source membership"):
        _catalog(tmp_path).register(result.generation_id)


def _v2_damage_extra(capture: dict, file: dict) -> None:
    extra = copy.deepcopy(file)
    extra.update(source_id="source:extra", relative_path="extra.py", sha256="e" * 64)
    capture["files"].append(extra)
    capture["files"].sort(key=lambda item: item["relative_path"])
    capture["policy"]["roots"] = sorted((*capture["policy"]["roots"], "extra.py"))


def _v2_damage_relative_path(capture: dict, file: dict) -> None:
    old_path = file["relative_path"]
    file["relative_path"] = "other.py"
    capture["policy"]["roots"] = sorted(
        "other.py" if root == old_path else root
        for root in capture["policy"]["roots"]
    )


_V2_CAPTURE_DAMAGE = {
    "missing": lambda capture, file: capture.update(files=[]),
    "extra": _v2_damage_extra,
    "source_id": lambda capture, file: file.update(source_id="source:other"),
    "relative_path": _v2_damage_relative_path,
    "sha256": lambda capture, file: file.update(sha256="f" * 64),
    "size": lambda capture, file: file["stat"].update(size=file["stat"]["size"] + 1),
}


@pytest.mark.parametrize("damage", sorted(_V2_CAPTURE_DAMAGE))
def test_catalog_binds_present_v2_capture_to_exact_source_membership(
    tmp_path: Path, damage: str
) -> None:
    from evidence_graph import GraphSchema
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    from tests.code_kernel_helpers import basic_graph_records, captured_snapshot_for_records

    records = basic_graph_records()
    snapshot = captured_snapshot_for_records(records)
    repository = tmp_path / "repository"
    repository.mkdir()
    result = build_full_generation(
        GenerationCatalog(tmp_path / "state"),
        generation_id=f"v2-capture-{damage}",
        graph_schema=GraphSchema.V2,
        snapshot=snapshot,
        code_capture=snapshot.code_capture,
        repository_scope=resolve_repository_scope(repository),
        activate=False,
        **records,
    )
    manifest_path = result.generation_path / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    capture = manifest["code_capture"]
    _V2_CAPTURE_DAMAGE[damage](capture, capture["files"][0])
    capture["membership_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {"files": capture["files"], "directories": capture["directories"]}
        )
    ).hexdigest()
    manifest_path.write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="source membership"):
        GenerationCatalog(tmp_path / "state").register(result.generation_id)


def test_manifest_schema_requires_code_capture_only_for_graph_v3(tmp_path: Path) -> None:
    import jsonschema

    schema_path = SCRIPTS / "schemas/evidence-graph-manifest-v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    catalog = _catalog(tmp_path)
    _directory, v2 = _publish_v2(catalog, "schema-v2")
    assert not list(validator.iter_errors(v2))
    v3 = dict(v2, graph_schema_version="evidence-graph/v3")
    errors = list(validator.iter_errors(v3))
    assert any("code_capture" in error.message for error in errors)


def test_manifest_schema_uses_approved_capture_maxima_and_nested_file_shape() -> None:
    schema = json.loads(
        (SCRIPTS / "schemas/evidence-graph-manifest-v1.json").read_text(encoding="utf-8")
    )["properties"]["code_capture"]
    limits = schema["properties"]["limits"]["properties"]
    assert {name: value["maximum"] for name, value in limits.items()} == {
        "max_files": 1_000_000,
        "max_file_bytes": 1024**3,
        "max_total_bytes": 16 * 1024**3,
        "max_entries": 5_000_000,
        "max_directories": 1_000_000,
        "max_depth": 256,
        "chunk_bytes": 8 * 1024 * 1024,
    }
    assert limits["max_depth"]["minimum"] == 1
    assert limits["chunk_bytes"]["minimum"] == 4096
    file_schema = schema["properties"]["files"]["items"]
    assert file_schema["properties"]["source_id"]["maxLength"] == 512
    import jsonschema

    validator = jsonschema.Draft202012Validator(file_schema)
    base_file = {
        "relative_path": "src/a.py",
        "sha256": "a" * 64,
        "stat": {
            "size": 1,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "mode": 1,
            "device": 1,
            "inode": 1,
        },
    }
    for source_id in ("x" * 512, "界" * 512):
        assert not list(validator.iter_errors({**base_file, "source_id": source_id}))
    for source_id in ("x" * 513, "界" * 513):
        assert list(validator.iter_errors({**base_file, "source_id": source_id}))
    assert set(file_schema["required"]) == {
        "source_id",
        "relative_path",
        "sha256",
        "stat",
    }
    assert set(file_schema["properties"]["stat"]["required"]) == {
        "size",
        "mtime_ns",
        "ctime_ns",
        "mode",
        "device",
        "inode",
    }


@pytest.mark.parametrize("graph_schema", [None, "other-graph/v1"])
def test_v2_requires_exact_evidence_graph_schema(tmp_path, graph_schema):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-wrong-graph")
    manifest["graph_schema_version"] = graph_schema
    if graph_schema is None:
        manifest["graph_extractor_version"] = None
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="graph schema|Evidence Graph"):
        catalog.register("v2-wrong-graph")


def test_v2_always_invokes_evidence_graph_semantic_validator(tmp_path, monkeypatch):
    import evidence_graph

    catalog = _catalog(tmp_path)
    _publish_v2(catalog, "v2-graph-validation")
    called = {}
    real_validate = evidence_graph.validate_generation_artifact

    def validate(*args, deadline=None, monotonic=None, cancelled=None, **kwargs):
        called.update(deadline=deadline, monotonic=monotonic, cancelled=cancelled)
        return real_validate(
            *args,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
            **kwargs,
        )

    monkeypatch.setattr(evidence_graph, "validate_generation_artifact", validate)

    catalog.register("v2-graph-validation")

    assert called["monotonic"] is not None
    assert called["deadline"] is None
    assert called["cancelled"] is None


def test_generic_register_and_activate_still_run_real_semantic_validators(
    tmp_path, monkeypatch
):
    import evidence_graph
    import search_memory

    catalog = _catalog(tmp_path)
    _publish_v2(catalog, "generic-full-validation")
    calls = {"evidence": 0, "fts": 0}
    real_evidence = evidence_graph.validate_generation_artifact
    real_fts = search_memory.validate_generation_fts_artifact

    def validate_evidence(*args, **kwargs):
        calls["evidence"] += 1
        return real_evidence(*args, **kwargs)

    def validate_fts(*args, **kwargs):
        calls["fts"] += 1
        return real_fts(*args, **kwargs)

    monkeypatch.setattr(evidence_graph, "validate_generation_artifact", validate_evidence)
    monkeypatch.setattr(search_memory, "validate_generation_fts_artifact", validate_fts)

    catalog.register("generic-full-validation")
    assert catalog.activate("generic-full-validation", expected_active=None)

    # Activation proves the same bytes rather than re-deriving what they mean:
    # every artifact was hashed against the manifest again, and a verdict about
    # identical bytes cannot differ. Different bytes are checked for themselves.
    assert calls == {"evidence": 1, "fts": 1}

    _publish_v2(catalog, "generic-full-validation-other")
    catalog.register("generic-full-validation-other")

    assert calls == {"evidence": 2, "fts": 2}


@pytest.mark.parametrize("mutation", ["content", "replacement", "manifest"])
def test_validated_candidate_rejects_post_validation_tampering(
    tmp_path, mutation
):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish_v2(catalog, f"candidate-{mutation}")
    candidate = catalog._validate_candidate(f"candidate-{mutation}")
    target = directory / ("manifest.json" if mutation == "manifest" else "evidence.sqlite3")
    original = target.read_bytes()

    if mutation == "content":
        _rewrite_preserving_metadata(target, bytes([original[0] ^ 1]) + original[1:])
    elif mutation == "replacement":
        replacement = directory.parent / "replacement.tmp"
        replacement.write_bytes(original)
        before = target.stat(follow_symlinks=False)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, target)
    else:
        changed = original.replace(b"fixture-graph/v1", b"fixture-graph/v2")
        assert changed != original
        _rewrite_preserving_metadata(target, changed)

    with pytest.raises((PermissionError, ValueError), match="changed|identity|validation"):
        catalog._register_validated(candidate)


def test_validated_candidate_is_bound_to_issuing_catalog_instance(tmp_path):
    first = _catalog(tmp_path)
    _publish_v2(first, "instance-bound")
    candidate = first._validate_candidate("instance-bound")
    second = _catalog(tmp_path)

    with pytest.raises(TypeError, match="issued by this catalog"):
        second._register_validated(candidate)


def test_validated_candidate_activation_rejects_catalog_registration_race(tmp_path):
    catalog = _catalog(tmp_path)
    _publish_v2(catalog, "registration-race")
    candidate = catalog._validate_candidate("registration-race")
    catalog._register_validated(candidate)
    with sqlite3.connect(catalog.catalog_path) as database:
        database.execute(
            "UPDATE generations SET manifest_json=?, manifest_sha256=? "
            "WHERE generation_id=?",
            (b"{}", hashlib.sha256(b"{}").hexdigest(), "registration-race"),
        )
        database.commit()

    with pytest.raises(ValueError, match="registration changed"):
        catalog._activate_validated(candidate, expected_active=None)
    assert catalog.get_active() is None


def test_validated_candidate_activation_rejects_post_registration_replacement(
    tmp_path,
):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish_v2(catalog, "activation-replacement")
    candidate = catalog._validate_candidate("activation-replacement")
    catalog._register_validated(candidate)
    target = directory / "evidence.sqlite3"
    replacement = directory.parent / "activation-replacement.tmp"
    replacement.write_bytes(target.read_bytes())
    before = target.stat(follow_symlinks=False)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, target)

    with pytest.raises((PermissionError, ValueError), match="changed|identity|validation"):
        catalog._activate_validated(candidate, expected_active=None)
    assert catalog.get_active() is None


def test_validated_candidate_rejects_catalog_file_replacement(tmp_path):
    catalog = _catalog(tmp_path)
    _publish_v2(catalog, "catalog-replacement")
    candidate = catalog._validate_candidate("catalog-replacement")
    replacement = catalog.catalog_path.with_name("replacement.sqlite3")
    replacement.write_bytes(catalog.catalog_path.read_bytes())
    os.replace(replacement, catalog.catalog_path)

    with pytest.raises(PermissionError, match="catalog|identity"):
        catalog._register_validated(candidate)


def test_validated_candidate_rechecks_replacement_inside_registration_transaction(
    tmp_path, monkeypatch
):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish_v2(catalog, "transaction-replacement")
    candidate = catalog._validate_candidate("transaction-replacement")
    target = directory / "evidence.sqlite3"
    replacement = directory.parent / "transaction-replacement.tmp"
    replacement.write_bytes(target.read_bytes())
    before = target.stat(follow_symlinks=False)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    real_transaction = catalog._write_transaction

    @contextmanager
    def replace_then_transact(deadline):
        os.replace(replacement, target)
        with real_transaction(deadline) as database:
            yield database

    monkeypatch.setattr(catalog, "_write_transaction", replace_then_transact)

    with pytest.raises((PermissionError, ValueError)):
        catalog._register_validated(candidate)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0


_MUTATION_BOUNDARIES = (
    "candidate-register",
    "candidate-activate",
    "public-register",
    "public-activate",
)

_BOUNDARY_CALLS = {
    "candidate-register": lambda catalog, generation_id, candidate: (
        catalog._register_validated(candidate)
    ),
    "candidate-activate": lambda catalog, generation_id, candidate: (
        catalog._activate_validated(candidate, expected_active=None)
    ),
    "public-register": lambda catalog, generation_id, candidate: (
        catalog.register(generation_id)
    ),
    "public-activate": lambda catalog, generation_id, candidate: (
        catalog.activate(generation_id, expected_active=None)
    ),
}


def _prepared_candidate(catalog, generation_id: str, boundary: str):
    if not boundary.startswith("candidate"):
        return None
    return catalog._validate_candidate(generation_id)


def _register_before_activation(catalog, generation_id: str, boundary: str, candidate) -> None:
    if not boundary.endswith("activate"):
        return
    if candidate is None:
        catalog.register(generation_id)
        return
    catalog._register_validated(candidate)


def _reach_boundary(catalog, generation_id: str, boundary: str, candidate) -> None:
    _BOUNDARY_CALLS[boundary](catalog, generation_id, candidate)


@pytest.mark.parametrize("boundary", _MUTATION_BOUNDARIES)
@pytest.mark.parametrize("mutation", ["in-place", "replacement"])
def test_catalog_mutation_boundaries_reject_metadata_preserving_tampering(
    tmp_path, monkeypatch, boundary, mutation
):
    catalog = _catalog(tmp_path)
    generation_id = boundary
    directory, _manifest = _publish(catalog, generation_id)
    candidate = _prepared_candidate(catalog, generation_id, boundary)
    _register_before_activation(catalog, generation_id, boundary, candidate)

    artifact = directory / "search.sqlite3"
    before = artifact.stat(follow_symlinks=False)
    changed = bytes([artifact.read_bytes()[0] ^ 1]) + artifact.read_bytes()[1:]
    replacement = directory.parent / f"{boundary}-replacement.tmp"
    replacement.write_bytes(artifact.read_bytes())
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    real_transaction = catalog._write_transaction

    @contextmanager
    def tamper_inside_transaction(deadline):
        with real_transaction(deadline) as database:
            _apply_artifact_mutation(mutation, artifact, changed, replacement)
            after = artifact.stat(follow_symlinks=False)
            assert os.path.samestat(before, after) is (mutation == "in-place")
            assert (after.st_size, after.st_mtime_ns) == (
                before.st_size,
                before.st_mtime_ns,
            )
            yield database

    monkeypatch.setattr(catalog, "_write_transaction", tamper_inside_transaction)

    with pytest.raises((PermissionError, ValueError)):
        _reach_boundary(catalog, generation_id, boundary, candidate)

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        registered = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
    assert registered == int(boundary.endswith("activate"))
    assert active is None


def _tamper_ready(acquiring: bool, chunk: bytes, tampered: bool) -> bool:
    return acquiring and bool(chunk) and not tampered


def _is_descriptor_for(descriptor: int, path: Path) -> bool:
    return os.path.samestat(os.fstat(descriptor), path.stat(follow_symlinks=False))


@pytest.mark.parametrize("boundary", _MUTATION_BOUNDARIES)
@pytest.mark.parametrize("mutation", ["in-place", "replacement"])
def test_catalog_mutation_boundaries_reject_earlier_member_change_during_later_hash(
    tmp_path, monkeypatch, boundary, mutation
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    generation_id = f"coherent-{boundary}-{mutation}"
    directory, _manifest = _publish(catalog, generation_id, extra_artifacts=1)
    candidate = _prepared_candidate(catalog, generation_id, boundary)
    _register_before_activation(catalog, generation_id, boundary, candidate)

    earlier = directory / "artifact-0000.bin"
    later = directory / "search.sqlite3"
    before = earlier.stat(follow_symlinks=False)
    original = earlier.read_bytes()
    changed = bytes([original[0] ^ 1]) + original[1:]
    replacement = directory.parent / f"{generation_id}-replacement.tmp"
    replacement.write_bytes(original)
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    real_read = generation_catalog.os.read
    real_acquire = catalog._acquire_seal_capability
    acquiring = False
    tampered = False

    def read_and_tamper(descriptor, size):
        nonlocal tampered
        chunk = real_read(descriptor, size)
        if _tamper_ready(acquiring, chunk, tampered) and _is_descriptor_for(
            descriptor, later
        ):
            _apply_artifact_mutation(mutation, earlier, changed, replacement)
            tampered = True
        return chunk

    def tracked_acquire(*args, **kwargs):
        nonlocal acquiring
        acquiring = True
        try:
            return real_acquire(*args, **kwargs)
        finally:
            acquiring = False

    monkeypatch.setattr(generation_catalog.os, "read", read_and_tamper)
    monkeypatch.setattr(catalog, "_acquire_seal_capability", tracked_acquire)

    with pytest.raises((PermissionError, ValueError)):
        _reach_boundary(catalog, generation_id, boundary, candidate)

    assert tampered


def test_catalog_writer_is_not_held_while_publication_content_hash_blocks(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "blocked-hash")
    other = generation_catalog.GenerationCatalog(catalog.state_root)
    real_read = generation_catalog.os.read
    real_acquire = catalog._acquire_seal_capability
    acquiring = False
    reads = 0
    hashing = threading.Event()
    release_hash = threading.Event()
    errors = []

    def blocking_read(descriptor, size):
        nonlocal reads
        chunk = real_read(descriptor, size)
        if acquiring and chunk:
            reads += 1
            if reads == 1:
                hashing.set()
                assert release_hash.wait(timeout=5)
        return chunk

    def tracked_acquire(*args, **kwargs):
        nonlocal acquiring
        acquiring = True
        try:
            return real_acquire(*args, **kwargs)
        finally:
            acquiring = False

    def register():
        try:
            catalog.register("blocked-hash")
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(generation_catalog.os, "read", blocking_read)
    monkeypatch.setattr(catalog, "_acquire_seal_capability", tracked_acquire)
    worker = threading.Thread(target=register)
    worker.start()
    assert hashing.wait(timeout=5)

    try:
        with other._write_transaction(time.monotonic() + 0.25) as database:
            database.execute(
                "UPDATE catalog_state SET active_generation_id=active_generation_id "
                "WHERE singleton=1"
            )
    finally:
        release_hash.set()
        worker.join(timeout=5)

    assert worker.is_alive() is False
    assert errors == []


def test_publication_keeps_seal_descriptors_open_until_catalog_commit(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "held-through-commit")
    events = []
    real_close = generation_catalog._GenerationSealCapability.close
    real_transaction = catalog._write_transaction

    def tracked_close(capability):
        assert all(os.fstat(held.descriptor) for held in capability.held_files)
        events.append("close")
        real_close(capability)

    @contextmanager
    def tracked_transaction(deadline):
        with real_transaction(deadline) as database:
            yield database
        events.append("commit")

    monkeypatch.setattr(
        generation_catalog._GenerationSealCapability, "close", tracked_close
    )
    monkeypatch.setattr(catalog, "_write_transaction", tracked_transaction)

    catalog.register("held-through-commit")

    assert events == ["commit", "close"]


def test_capability_close_attempts_every_descriptor_once_and_raises_first_error(
    tmp_path, monkeypatch
):
    import generation_catalog

    held_files = tuple(
        generation_catalog._HeldFile(tmp_path / str(descriptor), str(descriptor), descriptor, ())
        for descriptor in (11, 12, 13)
    )
    capability = generation_catalog._GenerationSealCapability(
        tmp_path,
        (),
        held_files,
        deadline=None,
        monotonic=time.monotonic,
        cancelled=None,
    )
    attempts = []

    def close(descriptor):
        assert capability._closed is False
        attempts.append(descriptor)
        if descriptor in {11, 13}:
            raise OSError(f"close-{descriptor}")

    monkeypatch.setattr(generation_catalog.os, "close", close)

    with pytest.raises(OSError, match="close-11"):
        capability.close()

    assert attempts == [11, 12, 13]
    assert capability._closed is True
    capability.close()
    assert attempts == [11, 12, 13]


def test_capability_context_close_error_precedes_body_and_closes_all_descriptors(
    tmp_path, monkeypatch
):
    import generation_catalog

    held_files = tuple(
        generation_catalog._HeldFile(tmp_path / str(descriptor), str(descriptor), descriptor, ())
        for descriptor in (21, 22)
    )
    capability = generation_catalog._GenerationSealCapability(
        tmp_path,
        (),
        held_files,
        deadline=None,
        monotonic=time.monotonic,
        cancelled=None,
    )
    attempts = []

    def close(descriptor):
        attempts.append(descriptor)
        raise OSError(f"close-{descriptor}")

    monkeypatch.setattr(generation_catalog.os, "close", close)

    with pytest.raises(OSError, match="close-21") as raised:
        with capability:
            raise ValueError("body failure")

    assert attempts == [21, 22]
    assert isinstance(raised.value.__context__, ValueError)
    assert str(raised.value.__context__) == "body failure"


def test_publication_rejects_membership_change_while_later_member_is_hashing(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "membership-race", extra_artifacts=1)
    later = directory / "search.sqlite3"
    injected = directory / "undeclared.bin"
    real_read = generation_catalog.os.read
    real_acquire = catalog._acquire_seal_capability
    acquiring = False
    changed = False

    def read_and_add_member(descriptor, size):
        nonlocal changed
        chunk = real_read(descriptor, size)
        if (
            acquiring
            and chunk
            and not changed
            and os.path.samestat(os.fstat(descriptor), later.stat(follow_symlinks=False))
        ):
            injected.write_bytes(b"undeclared")
            changed = True
        return chunk

    def tracked_acquire(*args, **kwargs):
        nonlocal acquiring
        acquiring = True
        try:
            return real_acquire(*args, **kwargs)
        finally:
            acquiring = False

    monkeypatch.setattr(generation_catalog.os, "read", read_and_add_member)
    monkeypatch.setattr(catalog, "_acquire_seal_capability", tracked_acquire)

    with pytest.raises((PermissionError, ValueError), match="changed|seal"):
        catalog.register("membership-race")

    assert changed


def test_artifact_hash_rejects_in_place_mutation_during_read_with_restored_mtime(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    payload = b"a" * (generation_catalog.HASH_CHUNK_BYTES + 1)
    directory, _manifest = _publish(catalog, "hash-race", payload=payload)
    artifact = directory / "search.sqlite3"
    real_read = generation_catalog.os.read
    mutated = False

    def read_then_mutate(descriptor, size):
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if chunk and not mutated:
            changed = bytes([payload[0] ^ 1]) + payload[1:]
            _rewrite_preserving_metadata(artifact, changed)
            mutated = True
        return chunk

    monkeypatch.setattr(generation_catalog.os, "read", read_then_mutate)

    with pytest.raises((PermissionError, ValueError), match="changed|wrong hash"):
        catalog.register("hash-race")


def test_validated_candidate_honors_cancellation_and_deadline(tmp_path):
    import generation_catalog

    cancelled_catalog = _catalog(tmp_path / "cancelled")
    _publish_v2(cancelled_catalog, "cancelled-candidate")
    cancelled_candidate = cancelled_catalog._validate_candidate("cancelled-candidate")
    with pytest.raises(TimeoutError, match="cancelled"):
        cancelled_catalog._register_validated(
            cancelled_candidate, cancelled=lambda: True
        )

    deadline_catalog = generation_catalog.GenerationCatalog(
        tmp_path / "deadline/state", clock=lambda: NOW, monotonic=lambda: 10.0
    )
    _publish_v2(deadline_catalog, "deadline-candidate")
    deadline_candidate = deadline_catalog._validate_candidate("deadline-candidate")
    with pytest.raises(TimeoutError, match="deadline"):
        deadline_catalog._register_validated(deadline_candidate, deadline=10.0)


def test_v2_requires_validated_repository_scope(tmp_path):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-unscoped")
    manifest.pop("repository_scope")
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="repository scope"):
        catalog.register("v2-unscoped")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE chunks SET content = content || ' tampered'",
        "UPDATE chunks SET chunk_id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "UPDATE chunks SET span_sha256 = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
        "UPDATE chunks SET byte_start = byte_start + 1",
        "UPDATE chunks SET byte_end = byte_end - 1",
        "UPDATE chunks SET line_start = line_start + 1",
        "UPDATE chunks SET line_end = line_end + 1",
        "UPDATE chunks SET source_sha256 = 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'",
        "UPDATE chunks SET source_id = 'source:other.md'",
        "UPDATE chunks SET source_path = 'knowledge/notes/other.md'",
        "UPDATE chunks SET parent_page = 'knowledge/notes/other.md'",
        "UPDATE chunks SET heading_ancestry = '[\"Other\"]'",
        "UPDATE chunks SET type = 'pattern'",
        "UPDATE chunks SET project = 'other-project'",
        "UPDATE chunks SET authority = 'web'",
        "UPDATE chunks SET confidence = 'low'",
        "UPDATE chunks SET status = 'superseded'",
        "UPDATE chunks SET valid_from = '2025-01-01'",
        "UPDATE chunks SET valid_to = '2028-01-01'",
        "UPDATE chunks SET language = 'ru'",
        "UPDATE chunks SET title = 'Other title'",
        "DELETE FROM chunks",
    ],
)
def test_v2_rejects_fts_rows_not_bound_to_captured_source_bytes(tmp_path, sql):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-tampered-chunk")
    with sqlite3.connect(directory / "search.sqlite3") as database:
        database.execute(sql)
        database.execute(
            "UPDATE generation_metadata SET value=(SELECT CAST(COUNT(*) AS TEXT) FROM chunks) "
            "WHERE key='chunk_count'"
        )
        database.commit()
    _refresh_artifact(directory, manifest, "search.sqlite3")

    with pytest.raises(ValueError, match="FTS|search"):
        catalog.register("v2-tampered-chunk")


def test_v2_rejects_extra_well_formed_stale_chunk_after_hash_recomputed(tmp_path):
    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-extra-chunk")
    with sqlite3.connect(directory / "search.sqlite3") as database:
        row = list(database.execute("SELECT * FROM chunks LIMIT 1").fetchone())
        row[0] = "d" * 64
        row[1] = 1
        database.execute("INSERT INTO chunks VALUES (" + ",".join("?" * 22) + ")", row)
        database.execute(
            "UPDATE generation_metadata SET value='2' WHERE key='chunk_count'"
        )
        database.commit()
    _refresh_artifact(directory, manifest, "search.sqlite3")

    with pytest.raises(ValueError, match="FTS|search"):
        catalog.register("v2-extra-chunk")


def test_public_fts_validator_rejects_graph_source_membership_drift(tmp_path):
    import search_memory

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-source-drift")
    with sqlite3.connect(directory / "evidence.sqlite3") as database:
        database.execute("UPDATE source SET relative_path='knowledge/notes/other.md'")
        database.commit()

    with pytest.raises(ValueError, match="source|membership"):
        search_memory.validate_generation_fts_artifact(
            directory,
            manifest,
            state_root=catalog.state_root,
        )


def test_public_fts_validator_cancels_during_source_manifest_stream(
    tmp_path, monkeypatch
):
    import search_memory

    catalog = _catalog(tmp_path)
    directory, manifest = _publish_v2(catalog, "v2-cancel-source-read")
    cancelled = False
    real_read = search_memory.os.read

    def read_then_cancel(descriptor, size):
        nonlocal cancelled
        content = real_read(descriptor, size)
        if content:
            cancelled = True
        return content

    monkeypatch.setattr(search_memory.os, "read", read_then_cancel)

    with pytest.raises(TimeoutError, match="cancel"):
        search_memory.validate_generation_fts_artifact(
            directory,
            manifest,
            state_root=catalog.state_root,
            cancelled=lambda: cancelled,
        )


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
    real_transaction = catalog._write_transaction

    @contextmanager
    def mutate_then_transact(deadline):
        with real_transaction(deadline) as database:
            _rewrite_preserving_metadata(manifest_path, changed)
            yield database

    monkeypatch.setattr(catalog, "_write_transaction", mutate_then_transact)

    with pytest.raises(ValueError, match="changed|seal"):
        catalog.register("gen-1", cancelled=lambda: False)
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


def test_manifest_accepts_optional_closed_repository_scope(tmp_path):
    from repository_scope import resolve_repository_scope

    root = tmp_path / "repository"
    root.mkdir()
    scope = resolve_repository_scope(root).as_dict()
    catalog = _catalog(tmp_path)
    _directory, manifest = _publish(catalog, "gen-1", repository_scope=scope)

    assert catalog.register("gen-1") == manifest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda scope: scope.pop("checkout_id"),
        lambda scope: scope.update(extra=True),
        lambda scope: scope.update(repository_id="repository:bad"),
        lambda scope: scope.update(checkout_id="checkout:" + "f" * 63),
        lambda scope: scope.update(checkout_root="relative/root"),
        lambda scope: scope.update(git_common_dir="relative/.git"),
        lambda scope: scope.update(schema_version="x" * 129),
        lambda scope: scope.update(checkout_root="C:/" + "x" * 4096),
    ],
)
def test_manifest_rejects_invalid_repository_scope(tmp_path, mutate):
    from reliable_memory import canonical_json_bytes
    from repository_scope import resolve_repository_scope

    root = tmp_path / "repository"
    root.mkdir()
    scope = resolve_repository_scope(root).as_dict()
    mutate(scope)
    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-1", repository_scope=scope)
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises((TypeError, ValueError)):
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


def test_manifest_accepts_bound_vector_metadata(tmp_path):
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
    )
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    assert catalog.register("gen-1") == manifest


def test_v1_manifest_rejects_legacy_graph_schema_alias(tmp_path):
    from reliable_memory import canonical_json_bytes

    catalog = _catalog(tmp_path)
    directory, manifest = _publish(catalog, "gen-legacy-graph")
    manifest.update(
        graph_schema_version="graph/v1",
        graph_extractor_version="graph-extractor/v1",
    )
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="graph schema|Evidence Graph"):
        catalog.register("gen-legacy-graph")


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


def test_discard_commit_failure_preserves_registration_and_directory(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    real_transaction = catalog._write_transaction

    @contextmanager
    def fail_commit(deadline):
        with real_transaction(deadline) as database:
            yield database
            raise sqlite3.OperationalError("injected discard commit failure")

    monkeypatch.setattr(catalog, "_write_transaction", fail_commit)

    with pytest.raises(sqlite3.OperationalError, match="injected discard commit failure"):
        catalog.discard_unactivated("gen-1")

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        rows = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
    assert rows == 1
    assert directory.is_dir()


def test_discard_cleanup_failure_leaves_unregistered_orphan_and_raises(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")

    def fail_cleanup(_path):
        raise OSError("injected recursive deletion failure")

    monkeypatch.setattr(generation_catalog.shutil, "rmtree", fail_cleanup)

    with pytest.raises(OSError, match="injected recursive deletion failure"):
        catalog.discard_unactivated("gen-1")

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        rows = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        history = database.execute(
            "SELECT COUNT(*) FROM activation_history WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
    assert (rows, active, history) == (0, None, 0)
    assert directory.is_dir()


def test_discard_parent_fsync_failure_cannot_restore_registration(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")

    def fail_fsync(path):
        assert Path(path) == catalog.generations_path
        raise OSError("injected parent fsync failure")

    monkeypatch.setattr(generation_catalog, "fsync_directory", fail_fsync)

    with pytest.raises(OSError, match="injected parent fsync failure"):
        catalog.discard_unactivated("gen-1")

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        state = (
            database.execute(
                "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
            ).fetchone()[0],
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute(
                "SELECT COUNT(*) FROM activation_history WHERE generation_id = 'gen-1'"
            ).fetchone()[0],
        )
    assert state == (0, None, 0)
    assert not directory.exists()


@pytest.mark.parametrize("reference", ["active", "history"])
def test_discard_refuses_active_or_historically_activated_generation(tmp_path, reference):
    catalog = _catalog(tmp_path)
    first, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)
    if reference == "history":
        _publish(catalog, "gen-2")
        catalog.register("gen-2")
        assert catalog.activate("gen-2", expected_active="gen-1")

    assert catalog.discard_unactivated("gen-1") is False

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        rows = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
    assert rows == 1
    assert first.is_dir()


def test_discard_cancellation_is_cooperative_after_catalog_unregistration(tmp_path):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1", extra_artifacts=4)
    catalog.register("gen-1")
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(TimeoutError, match="cancel"):
        catalog.discard_unactivated("gen-1", cancelled=cancelled)

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        rows = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
    assert rows == 0
    assert directory.exists()


def test_repeated_expired_discard_deadlines_do_not_consume_generation_rows(
    tmp_path, monkeypatch
):
    import generation_catalog

    monotonic = _Monotonic()
    catalog = generation_catalog.GenerationCatalog(
        tmp_path / "state", clock=lambda: NOW, monotonic=monotonic
    )
    monkeypatch.setattr(generation_catalog, "MAX_GENERATIONS", 2)

    for number in range(3):
        generation_id = f"expired-{number}"
        directory, _manifest = _publish(catalog, generation_id)
        monotonic.value = 0.0
        catalog.register(generation_id, deadline=1.0)
        monotonic.value = 2.0

        with pytest.raises(TimeoutError, match="deadline"):
            catalog.discard_unactivated(generation_id, deadline=1.0)

        with closing(sqlite3.connect(catalog.catalog_path)) as database:
            assert database.execute(
                "SELECT COUNT(*) FROM generations"
            ).fetchone()[0] == 0
        assert directory.is_dir()


def test_discard_fences_prevalidated_concurrent_registration(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    prevalidated = threading.Event()
    allow_registration = threading.Event()
    errors = []
    real_acquire = catalog._acquire_seal_capability
    real_rmtree = generation_catalog.shutil.rmtree

    def pause_after_prevalidation(*args, **kwargs):
        result = real_acquire(*args, **kwargs)
        if threading.current_thread().name == "racing-registration":
            prevalidated.set()
            assert allow_registration.wait(timeout=5)
        return result

    def release_registration_then_remove(path):
        allow_registration.set()
        real_rmtree(path)

    def register_again():
        try:
            catalog.register("gen-1")
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(catalog, "_acquire_seal_capability", pause_after_prevalidation)
    monkeypatch.setattr(
        generation_catalog.shutil, "rmtree", release_registration_then_remove
    )
    worker = threading.Thread(target=register_again, name="racing-registration")
    worker.start()
    assert prevalidated.wait(timeout=5)

    assert catalog.discard_unactivated("gen-1") is True
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert errors and isinstance(errors[0], (FileNotFoundError, ValueError))
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        rows = database.execute(
            "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
        ).fetchone()[0]
    assert rows == 0
    assert not directory.exists()


def test_discard_serializes_with_activation_and_leaves_no_dangling_reference(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    activation_waiting = threading.Event()
    allow_activation = threading.Event()
    activation_errors = []
    real_acquire = catalog._acquire_seal_capability
    real_rmtree = generation_catalog.shutil.rmtree

    def pause_after_activation_validation(*args, **kwargs):
        result = real_acquire(*args, **kwargs)
        if threading.current_thread().name == "racing-activation":
            activation_waiting.set()
            assert allow_activation.wait(timeout=5)
        return result

    def activate():
        try:
            catalog.activate("gen-1", expected_active=None)
        except BaseException as exc:
            activation_errors.append(exc)

    def race_then_remove(path):
        allow_activation.set()
        real_rmtree(path)

    monkeypatch.setattr(
        catalog, "_acquire_seal_capability", pause_after_activation_validation
    )
    monkeypatch.setattr(generation_catalog.shutil, "rmtree", race_then_remove)
    worker = threading.Thread(target=activate, name="racing-activation")
    worker.start()
    assert activation_waiting.wait(timeout=5)

    assert catalog.discard_unactivated("gen-1") is True
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert activation_errors and isinstance(
        activation_errors[0], (FileNotFoundError, ValueError)
    )
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        state = (
            database.execute(
                "SELECT COUNT(*) FROM generations WHERE generation_id = 'gen-1'"
            ).fetchone()[0],
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute(
                "SELECT COUNT(*) FROM activation_history WHERE generation_id = 'gen-1'"
            ).fetchone()[0],
        )
    assert state == (0, None, 0)
    assert not directory.exists()


def test_activation_rechecks_validation_seal_inside_cas_transaction(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "gen-1")
    catalog.register("gen-1")
    artifact = directory / "search.sqlite3"

    real_transaction = catalog._write_transaction

    @contextmanager
    def mutate_then_transact(deadline):
        with real_transaction(deadline) as database:
            _rewrite_preserving_metadata(artifact, b"SEARCH-INDEX")
            yield database

    monkeypatch.setattr(catalog, "_write_transaction", mutate_then_transact)

    with pytest.raises(ValueError, match="changed|seal"):
        catalog.activate("gen-1", expected_active=None)
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        active = database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0]
        history = database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0]
    assert active is None
    assert history == 0


def test_activation_hashes_outside_writer_and_revalidates_inside_transaction(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")
    in_transaction = False
    hash_locations = []
    revalidation_locations = []
    real_hash = generation_catalog._hash_descriptor
    real_revalidate = generation_catalog._GenerationSealCapability.revalidate
    real_transaction = catalog._write_transaction

    def checked_hash(*args, **kwargs):
        hash_locations.append(in_transaction)
        return real_hash(*args, **kwargs)

    def checked_revalidation(self):
        revalidation_locations.append(in_transaction)
        return real_revalidate(self)

    @contextmanager
    def tracked_transaction(deadline):
        nonlocal in_transaction
        with real_transaction(deadline) as database:
            in_transaction = True
            try:
                yield database
            finally:
                in_transaction = False

    monkeypatch.setattr(generation_catalog, "_hash_descriptor", checked_hash)
    monkeypatch.setattr(
        generation_catalog._GenerationSealCapability, "revalidate", checked_revalidation
    )
    monkeypatch.setattr(catalog, "_write_transaction", tracked_transaction)

    assert catalog.activate("gen-1", expected_active=None)
    assert hash_locations and not any(hash_locations)
    assert revalidation_locations[-1] is True


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


def test_open_existing_read_only_avoids_catalog_setup_writes(tmp_path, monkeypatch):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)
    before = catalog.catalog_path.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only catalog performed setup writes")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(generation_catalog, "fsync_directory", forbidden)
    monkeypatch.setattr(generation_catalog, "open_operational_db", forbidden)
    monkeypatch.setattr(generation_catalog.GenerationCatalog, "_ensure_schema", forbidden)

    reader = generation_catalog.GenerationCatalog.open_existing_read_only(
        catalog.state_root,
        catalog_path=catalog.catalog_path,
    )

    assert reader.get_active()["generation_id"] == "gen-1"
    assert catalog.catalog_path.read_bytes() == before


def test_open_existing_read_only_checks_cancellation_before_path_resolution(
    tmp_path, monkeypatch
):
    import generation_catalog

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cancelled open resolved a path")

    monkeypatch.setattr(Path, "resolve", forbidden)

    with pytest.raises(TimeoutError, match="cancelled"):
        generation_catalog.GenerationCatalog.open_existing_read_only(
            tmp_path,
            cancelled=lambda: True,
        )


@pytest.mark.parametrize("cancel_after_resolves", [1, 2])
def test_open_existing_read_only_checks_cancellation_after_each_path_resolution(
    tmp_path, monkeypatch, cancel_after_resolves
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    original_resolve = Path.resolve
    state = {"cancelled": False, "resolves": 0}

    def tracked_resolve(path, *args, **kwargs):
        resolved = original_resolve(path, *args, **kwargs)
        state["resolves"] += 1
        if state["resolves"] == cancel_after_resolves:
            state["cancelled"] = True
        return resolved

    def forbidden_open(_catalog):
        raise AssertionError("cancelled open reached SQLite")

    monkeypatch.setattr(Path, "resolve", tracked_resolve)
    monkeypatch.setattr(
        generation_catalog.GenerationCatalog,
        "_readonly",
        forbidden_open,
    )

    with pytest.raises(TimeoutError, match="cancelled"):
        generation_catalog.GenerationCatalog.open_existing_read_only(
            catalog.state_root,
            catalog_path=catalog.catalog_path,
            cancelled=lambda: state["cancelled"],
        )

    assert state["resolves"] == cancel_after_resolves


def test_open_existing_read_only_rechecks_deadline_after_sqlite_open(
    tmp_path, monkeypatch
):
    import generation_catalog

    catalog = _catalog(tmp_path)
    state = {"closed": False, "opened": False}

    class Handle:
        def close(self):
            state["closed"] = True

    def delayed_open(_catalog):
        state["opened"] = True
        return Handle()

    monkeypatch.setattr(
        generation_catalog.GenerationCatalog,
        "_readonly",
        delayed_open,
    )

    with pytest.raises(TimeoutError, match="deadline"):
        generation_catalog.GenerationCatalog.open_existing_read_only(
            catalog.state_root,
            catalog_path=catalog.catalog_path,
            deadline=1.0,
            monotonic=lambda: 2.0 if state["opened"] else 0.0,
        )

    assert state == {"closed": True, "opened": True}


def test_read_only_catalog_fallback_does_not_repair_active_pointer(tmp_path):
    import generation_catalog

    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    _publish(catalog, "gen-2", parent="gen-1")
    for generation_id in ("gen-1", "gen-2"):
        catalog.register(generation_id)
    assert catalog.activate("gen-1", expected_active=None)
    assert catalog.activate("gen-2", expected_active="gen-1")
    (catalog.generations_path / "gen-2/search.sqlite3").write_bytes(b"corrupt")
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        before = (
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0],
        )

    reader = generation_catalog.GenerationCatalog.open_existing_read_only(
        catalog.state_root,
        catalog_path=catalog.catalog_path,
    )
    selected = reader.get_active()

    assert selected is not None
    assert selected["generation_id"] == "gen-1"
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        after = (
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0],
        )
    assert after == before == ("gen-2", 2)


def test_read_only_catalog_rejects_write_transactions(tmp_path):
    import generation_catalog

    catalog = _catalog(tmp_path)
    reader = generation_catalog.GenerationCatalog.open_existing_read_only(
        catalog.state_root,
        catalog_path=catalog.catalog_path,
    )

    with pytest.raises(PermissionError, match="read-only"):
        with reader._write_transaction(None):
            raise AssertionError("read-only transaction body must not run")


@pytest.mark.parametrize("active_binding", ["foreign", "unbound"])
def test_get_active_for_repository_rejects_ineligible_active_without_mutation(
    tmp_path, active_binding
):
    from repository_scope import resolve_repository_scope

    requested_repository = tmp_path / "requested"
    foreign_repository = tmp_path / "foreign"
    requested_repository.mkdir()
    foreign_repository.mkdir()
    requested_scope = resolve_repository_scope(requested_repository)
    foreign_scope = resolve_repository_scope(foreign_repository)
    catalog = _catalog(tmp_path)
    _publish(catalog, "requested", repository_scope=requested_scope.as_dict())
    _publish(
        catalog,
        "active",
        parent="requested",
        repository_scope=(
            foreign_scope.as_dict() if active_binding == "foreign" else None
        ),
    )
    for generation_id in ("requested", "active"):
        catalog.register(generation_id)
    assert catalog.activate("requested", expected_active=None)
    assert catalog.activate("active", expected_active="requested")
    (catalog.generations_path / "active/search.sqlite3").write_bytes(b"corrupt")
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        before = (
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0],
        )

    assert catalog.get_active_for_repository(requested_scope) is None

    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        after = (
            database.execute(
                "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
            ).fetchone()[0],
            database.execute("SELECT COUNT(*) FROM activation_history").fetchone()[0],
        )
    assert after == before


def test_get_active_for_repository_repairs_only_to_same_scope_fallback(tmp_path):
    from repository_scope import resolve_repository_scope

    repository = tmp_path / "repository"
    repository.mkdir()
    scope = resolve_repository_scope(repository)
    catalog = _catalog(tmp_path)
    _publish(catalog, "prior", repository_scope=scope.as_dict())
    _publish(catalog, "active", parent="prior", repository_scope=scope.as_dict())
    for generation_id in ("prior", "active"):
        catalog.register(generation_id)
    assert catalog.activate("prior", expected_active=None)
    assert catalog.activate("active", expected_active="prior")
    (catalog.generations_path / "active/search.sqlite3").write_bytes(b"corrupt")

    selected = catalog.get_active_for_repository(scope)

    assert selected is not None
    assert selected["generation_id"] == "prior"
    with closing(sqlite3.connect(catalog.catalog_path)) as database:
        assert database.execute(
            "SELECT active_generation_id FROM catalog_state WHERE singleton = 1"
        ).fetchone()[0] == "prior"


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

    def mutate_and_check(generation_path, seal, **kwargs):
        _rewrite_preserving_metadata(fallback_artifact, b"SEARCH-INDEX")
        return real_check(generation_path, seal, **kwargs)

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
    in_transaction = False
    real_transaction = catalog._write_transaction
    real_revalidate = generation_catalog._GenerationSealCapability.revalidate

    @contextmanager
    def tracked_transaction(deadline):
        nonlocal in_transaction
        with real_transaction(deadline) as database:
            in_transaction = True
            try:
                yield database
            finally:
                in_transaction = False

    def expire_after_seal(self):
        valid = real_revalidate(self)
        if in_transaction:
            monotonic.value = 2.0
        return valid

    monkeypatch.setattr(catalog, "_write_transaction", tracked_transaction)
    monkeypatch.setattr(
        generation_catalog._GenerationSealCapability, "revalidate", expire_after_seal
    )

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


def test_get_active_propagates_cancellation_through_validation(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")

    with pytest.raises(TimeoutError, match="cancelled"):
        catalog.get_active(cancelled=lambda: True)


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


def test_a_writer_without_a_deadline_waits_out_contention(tmp_path, monkeypatch):
    """Two compare-and-swaps must not surface `database is locked`.

    A full local run produced exactly that: the loser's five-second busy window
    expired while the winner held the write lock, and the raw SQLite error
    reached the caller instead of a decided race.
    """
    import generation_catalog

    catalog = _catalog(tmp_path)
    seen: list[int] = []
    real_open = generation_catalog.open_operational_db

    def record(path, *, busy_ms, **options):
        seen.append(busy_ms)
        return real_open(path, busy_ms=busy_ms, **options)

    monkeypatch.setattr(generation_catalog, "open_operational_db", record)
    catalog._connect(deadline=None).close()

    assert seen == [generation_catalog.UNBOUNDED_BUSY_MS]
    assert generation_catalog.UNBOUNDED_BUSY_MS > generation_catalog.BUSY_MS


def test_a_reader_waits_out_an_exclusive_lock_instead_of_failing(tmp_path):
    """A concurrent commit must delay a read, never turn it into an error."""
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1")
    catalog.register("gen-1")
    catalog.activate("gen-1", expected_active=None)

    locked = threading.Event()
    released = threading.Event()

    def hold_exclusive() -> None:
        with closing(sqlite3.connect(str(catalog.catalog_path), isolation_level=None)) as holder:
            holder.execute("BEGIN EXCLUSIVE")
            locked.set()
            time.sleep(0.5)
            holder.execute("ROLLBACK")
        released.set()

    worker = threading.Thread(target=hold_exclusive)
    worker.start()
    try:
        assert locked.wait(10)
        assert not released.is_set()
        active = catalog.get_active()
    finally:
        worker.join()

    assert released.is_set()
    assert active is not None
    assert active["generation_id"] == "gen-1"


def _scope_at_commit(tmp_path: Path, commit: str):
    """A Git-flavoured scope for a temporary checkout, at a chosen commit."""
    from repository_scope import (
        SCHEMA_VERSION,
        RepositoryScope,
        derive_checkout_id,
        derive_repository_id,
        resolve_repository_scope,
    )

    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    checkout_root = resolve_repository_scope(repository).checkout_root
    git_common_dir = f"{checkout_root}/.git"
    repository_id = derive_repository_id(
        checkout_root=checkout_root, git_common_dir=git_common_dir
    )
    return RepositoryScope(
        SCHEMA_VERSION,
        repository_id,
        derive_checkout_id(repository_id, checkout_root),
        checkout_root,
        git_common_dir,
        commit,
    )


def test_a_generation_stays_eligible_after_the_repository_moves_to_a_new_commit(tmp_path):
    """A commit says when a generation was built, not where it belongs (NEW-65)."""
    built_at = _scope_at_commit(tmp_path, "a" * 40)
    asked_at = _scope_at_commit(tmp_path, "b" * 40)
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1", repository_scope=built_at.as_dict())
    catalog.register("gen-1")
    assert catalog.activate("gen-1", expected_active=None)

    selected = catalog.get_active_for_repository(asked_at)

    assert selected is not None
    assert selected["generation_id"] == "gen-1"
