"""A workspace-sensitive source is invalidated by its own universe, not by any.

`workspace_sensitive` marks a source whose extraction left an unresolved
reference it could not attribute to named candidates. Re-running it whenever
the thing it resolves against might have moved is correct and deliberately
conservative. Re-running it when a *different* extraction universe moved is
neither: `doctor._SourceExtractionAdapter` hands `extract_code` only
non-`knowledge/` sources and `extract_knowledge` only `knowledge/` sources, so
a knowledge page cannot answer a code reference and vice versa.

Measured on this vault before the change: appending one line to one knowledge
page rebuilt 400 code sources of 860. See
`docs/research/2026-08-29-what-a-changed-source-may-invalidate.md`.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CODE_PATH = "scripts/module.py"
KNOWLEDGE_PATH = "knowledge/notes/page.md"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _document(name: str) -> bytes:
    return json.dumps({"name": name}).encode()


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


class _Extractor:
    """One node per source, no cross-source references, chosen owners sensitive."""

    def __init__(self, *, sensitive: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.sensitive = frozenset(sensitive)

    def __call__(self, source, content, *, sources, source_bytes, deadline, cancelled):
        from evidence_graph_builder import SourceExtraction

        del sources, source_bytes, deadline, cancelled
        source_id = str(source["source_id"])
        self.calls.append(source_id)
        name = str(json.loads(content)["name"])
        digest = _sha(content)
        return SourceExtraction(
            nodes=(
                {
                    "node_id": f"node:{source_id}",
                    "kind": "fixture",
                    "identity_scheme": "fixture/v1",
                    "identity_key": source_id,
                    "metadata": {"name": name},
                },
            ),
            occurrences=(
                {
                    "occurrence_id": f"occurrence:{source_id}",
                    "node_id": f"node:{source_id}",
                    "source_id": source_id,
                    "role": "definition",
                    "byte_start": 0,
                    "byte_end": 1,
                    "line_start": 1,
                    "line_end": 1,
                },
            ),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            source_dependencies=(),
            workspace_sensitive=source_id in self.sensitive,
            invalidation_fingerprints={
                key: _sha(f"{key}:{digest}".encode())
                for key in ("exports", "imports", "signatures", "aliases", "project_metadata")
            },
        )


def _build(catalog, generation_id, files, extractor, *, parent=None, config=None):
    from evidence_graph_builder import build_incremental_generation

    sources = [_source(sid, path, content) for sid, (path, content) in files.items()]
    source_bytes = {sid: content for sid, (_path, content) in files.items()}
    return build_incremental_generation(
        catalog,
        sources=sources,
        source_bytes=source_bytes,
        extractor=extractor,
        reuse_config=config or _config(),
        generation_id=generation_id,
        parent_generation_id=parent,
        expected_active=parent,
    )


def _catalog(tmp_path):
    from generation_catalog import GenerationCatalog

    return GenerationCatalog(tmp_path / "state")


def _files(**overrides) -> dict[str, tuple[str, bytes]]:
    files = {
        "code-watcher": (CODE_PATH, _document("code-watcher")),
        "code-plain": ("scripts/plain.py", _document("code-plain")),
        "know-watcher": (KNOWLEDGE_PATH, _document("know-watcher")),
        "know-plain": ("knowledge/notes/plain.md", _document("know-plain")),
    }
    files.update(overrides)
    return files


SENSITIVE = ("code-watcher", "know-watcher")


def _seeded(tmp_path):
    catalog = _catalog(tmp_path)
    _build(catalog, "gen-1", _files(), _Extractor(sensitive=SENSITIVE))
    return catalog


def test_a_knowledge_edit_leaves_the_code_workspace_alone(tmp_path):
    catalog = _seeded(tmp_path)
    extractor = _Extractor(sensitive=SENSITIVE)

    result = _build(
        catalog,
        "gen-2",
        _files(**{"know-plain": ("knowledge/notes/plain.md", _document("know-plain-v2"))}),
        extractor,
        parent="gen-1",
    )

    assert set(result.rebuilt_sources) == {"know-plain", "know-watcher"}
    assert set(result.reused_sources) == {"code-plain", "code-watcher"}


def test_a_code_edit_leaves_the_knowledge_universe_alone(tmp_path):
    catalog = _seeded(tmp_path)
    extractor = _Extractor(sensitive=SENSITIVE)

    result = _build(
        catalog,
        "gen-2",
        _files(**{"code-plain": ("scripts/plain.py", _document("code-plain-v2"))}),
        extractor,
        parent="gen-1",
    )

    assert set(result.rebuilt_sources) == {"code-plain", "code-watcher"}
    assert set(result.reused_sources) == {"know-plain", "know-watcher"}


def test_an_added_knowledge_page_still_invalidates_the_knowledge_watchers(tmp_path):
    """The event that can resolve a dangling wikilink is a page appearing."""
    catalog = _seeded(tmp_path)
    extractor = _Extractor(sensitive=SENSITIVE)

    result = _build(
        catalog,
        "gen-2",
        _files(**{"know-new": ("knowledge/notes/new.md", _document("know-new"))}),
        extractor,
        parent="gen-1",
    )

    assert set(result.rebuilt_sources) == {"know-new", "know-watcher"}
    assert set(result.reused_sources) == {"code-plain", "code-watcher", "know-plain"}


def test_a_deleted_knowledge_page_still_invalidates_the_knowledge_watchers(tmp_path):
    catalog = _seeded(tmp_path)
    extractor = _Extractor(sensitive=SENSITIVE)
    files = _files()
    del files["know-plain"]

    result = _build(catalog, "gen-2", files, extractor, parent="gen-1")

    assert set(result.rebuilt_sources) == {"know-watcher"}
    assert set(result.reused_sources) == {"code-plain", "code-watcher"}


def test_a_code_membership_change_no_longer_hides_a_knowledge_edit(tmp_path):
    """`membership_changed` used to suppress every workspace invalidation.

    The suppression is harmless for the code universe, whose whole membership
    is rebuilt anyway, and wrong for the knowledge universe, which that
    rebuild does not cover.
    """
    catalog = _seeded(tmp_path)
    extractor = _Extractor(sensitive=SENSITIVE)

    result = _build(
        catalog,
        "gen-2",
        _files(
            **{
                "code-new": ("scripts/new.py", _document("code-new")),
                "know-plain": ("knowledge/notes/plain.md", _document("know-plain-v2")),
            }
        ),
        extractor,
        parent="gen-1",
    )

    assert "know-watcher" in result.rebuilt_sources
    assert set(result.rebuilt_sources) == {
        "code-new",
        "code-plain",
        "code-watcher",
        "know-plain",
        "know-watcher",
    }


class _Record:
    def __init__(self, path: str) -> None:
        self.relative_path = path
        self.logical_id = path
        self.language = "python"


class _Captured:
    def __init__(self, path: str) -> None:
        self.record = _Record(path)
        self.content = b""


class _Snapshot:
    def __init__(self, paths) -> None:
        self.sources = [_Captured(path) for path in paths]


BOUNDARY_PATHS = (
    "scripts/module.py",
    "tests/test_module.py",
    "knowledge/notes/page.md",
    "knowledge/projects/thing/journal.md",
)


def _extraction_paths(selected) -> set[str]:
    return {item.record.relative_path for item in selected}


def test_the_builder_and_the_extractor_agree_on_where_the_boundary_is():
    """The narrowing is sound only while the extractor splits on the same line.

    `_workspace_source_ids` is the builder's declaration of the code universe.
    `doctor._SourceExtractionAdapter` is the only extractor any production or
    benchmark caller passes, and it routes by the same prefix. If that ever
    stops being true, this test fails before a wrong reuse ships.
    """
    import doctor
    import evidence_graph_builder as builder

    snapshot = _Snapshot(BOUNDARY_PATHS)
    code = _extraction_paths(doctor._code_extraction_sources(snapshot))
    knowledge = _extraction_paths(doctor._knowledge_extraction_sources(snapshot))
    workspace = builder._workspace_source_ids(
        {path: {"relative_path": path} for path in BOUNDARY_PATHS}
    )

    assert code == workspace
    assert not (knowledge & workspace)
