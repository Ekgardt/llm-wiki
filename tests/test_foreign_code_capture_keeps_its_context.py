"""A sealed code-root policy survives strict rederivation and published reads."""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import time
from dataclasses import asdict

import corpus_snapshot as corpus
import pytest
import search_memory as search

from tests.slow_machine import LONG_TIMEOUT


class CodeAuthority(dict):
    code_roots = ("knowledge",)


@pytest.mark.parametrize("relative", [
    "knowledge/notes/README.md", "knowledge/daily/README.md",
    "knowledge/projects/demo/context.md", "knowledge/raw/sessions/example.md",
])
def test_strict_fts_rederivation_keeps_foreign_code_metadata(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("# Repository documentation\n\nExact physical evidence.\n")
    snapshot = corpus.collect_corpus(
        tmp_path, code_roots=("knowledge",), approved_code_roots=("knowledge",)
    )
    directory = tmp_path / "artifacts"
    directory.mkdir()
    artifact = search.build_generation_fts(snapshot, directory)
    manifest = {"collector_version": snapshot.collector_version,
                "extractor_version": snapshot.extractor_version,
                "source_manifest_sha256": snapshot.corpus_sha256, "artifacts": [artifact]}
    sources = CodeAuthority({source.record.logical_id: {
        **asdict(source.record), "content": source.content
    } for source in snapshot.sources})
    with contextlib.closing(sqlite3.connect(directory / search.GENERATION_FTS_ARTIFACT)) as database:
        assert search._valid_generation_fts(database, manifest, authoritative_sources=sources)


@pytest.mark.parametrize("root,expected", [
    ("knowledge", "doc"), ("knowledge/notes/page.md", "doc"),
    ("knowledge/notes/page", "note"), ("knowledge/notes/page.md.other", "note"),
])
def test_canonical_source_uses_component_containment(root, expected):
    import hashlib

    path = "knowledge/notes/page.md"
    content = b"# Page\n\nEvidence.\n"
    source = corpus.canonical_captured_source(
        source_id=f"source:{path}", source_path=path,
        source_sha256=hashlib.sha256(content).hexdigest(), content=content,
        code_roots=(root,),
    )
    assert source.metadata.type == expected


def test_context_does_not_bypass_expired_deadline():
    import hashlib

    content = b"# Page\nEvidence.\n"
    with pytest.raises(TimeoutError):
        corpus.canonical_retrieval_chunks(
            source_id="source:knowledge/notes/a.md", source_path="knowledge/notes/a.md",
            source_sha256=hashlib.sha256(content).hexdigest(), content=content,
            code_roots=("knowledge",), deadline=time.monotonic() - 1,
        )


@pytest.fixture
def sealed_foreign_generation(tmp_path, monkeypatch):
    from doctor import _generation_source_rows
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    target = vault / "knowledge/notes/page.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Code documentation\n\nPhysical bytes preserved.\n")
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    snapshot = corpus.collect_corpus(
        vault, code_roots=("knowledge",), approved_code_roots=("knowledge",)
    )
    state = tmp_path / "state"
    catalog = GenerationCatalog(state)
    build_full_generation(
        catalog, sources=_generation_source_rows(snapshot),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=[], occurrences=[], assertions=[], evidence=[], observations=[], dependencies=[],
        generation_id="foreign-context", snapshot=snapshot, publication_root=vault,
        repository_scope=resolve_repository_scope(vault),
    )
    monkeypatch.setattr(search, "STATE_ROOT", state)
    manifest = catalog.get_active()
    return vault, snapshot, state, catalog.generations_path / "foreign-context", manifest


def _authority(fixture, manifest=None):
    _vault, _snapshot, state, directory, original = fixture
    return search._generation_authoritative_sources(
        directory, original if manifest is None else manifest,
        state_root=state, deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None,
    )


def test_authority_uses_sealed_policy_not_top_level_override(sealed_foreign_generation):
    manifest = dict(sealed_foreign_generation[-1])
    manifest["code_roots"] = []
    authority = _authority(sealed_foreign_generation, manifest)
    assert authority.code_roots == ("knowledge",)
    with pytest.raises(AttributeError):
        authority.code_roots = ()


def test_published_sources_use_same_sealed_context(sealed_foreign_generation):
    _vault, original, _state, directory, _manifest = sealed_foreign_generation
    authority = _authority(sealed_foreign_generation)
    restored = search._published_sources(directory, authority, time.monotonic() + LONG_TIMEOUT)
    assert tuple(restored.values()) == original.sources


def test_altered_sealed_policy_refuses(sealed_foreign_generation):
    import json

    from reliable_memory import canonical_json_bytes

    directory = sealed_foreign_generation[3]
    path = directory / "source-manifest.json"
    document = json.loads(path.read_bytes())
    document["policy"]["code_roots"] = []
    path.write_bytes(canonical_json_bytes(document))
    with pytest.raises(ValueError):
        _authority(sealed_foreign_generation)


def test_published_snapshot_preserves_foreign_capture_policy(sealed_foreign_generation):
    vault, original, _state, directory, manifest = sealed_foreign_generation
    header = search._validated_source_manifest(
        directory, manifest, state_root=_state, deadline=time.monotonic() + LONG_TIMEOUT, cancelled=None
    )
    assert search._published_policy(header["policy"], vault).code_roots == original.policy.code_roots


def test_published_full_corpus_matches_original_foreign_snapshot(sealed_foreign_generation):
    from generation_catalog import GenerationCatalog

    vault, original, state, directory, manifest = sealed_foreign_generation
    restored = search._read_published_corpus(
        GenerationCatalog(state), directory, manifest, vault, time.monotonic() + LONG_TIMEOUT
    )
    assert restored.sources == original.sources
    assert restored.chunks == original.chunks
    assert restored.policy.code_roots == original.policy.code_roots


def test_historical_absent_top_level_roots_uses_sealed_policy(sealed_foreign_generation):
    manifest = dict(sealed_foreign_generation[-1])
    manifest.pop("code_roots", None)
    assert _authority(sealed_foreign_generation, manifest).code_roots == ("knowledge",)


def test_context_does_not_bypass_source_hash():
    with pytest.raises(ValueError, match="canonical hash"):
        corpus.canonical_captured_source(
            source_id="source:knowledge/notes/a.md", source_path="knowledge/notes/a.md",
            source_sha256="0" * 64, content=b"# Actual source\n",
            code_roots=("knowledge",),
        )


def test_context_does_not_bypass_cancellation():
    import hashlib

    content = b"# Actual source\n"
    with pytest.raises(TimeoutError):
        corpus.canonical_retrieval_chunks(
            source_id="source:knowledge/notes/a.md", source_path="knowledge/notes/a.md",
            source_sha256=hashlib.sha256(content).hexdigest(), content=content,
            code_roots=("knowledge",), cancelled=lambda: True,
        )
