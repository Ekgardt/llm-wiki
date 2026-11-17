"""Helpers shared by test_evidence_graph_incremental.py and the test files importing them."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


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
    def __init__(self, *, workspace_sensitive: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.workspace_sensitive = frozenset(workspace_sensitive)

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

        del deadline, sources
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
            workspace_sensitive=source_id in self.workspace_sensitive,
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
