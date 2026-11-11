"""Helpers shared by test_impact_analysis.py and the test files importing them."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "impact@example.test")
    _git(root, "config", "user.name", "Impact Test")
    (root / "alpha.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (root / "deleted.py").write_text("def removed():\n    return 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


def _path_selected(node: dict, path) -> bool:
    return path is None or node["metadata"].get("path") == path


def _kind_selected(node: dict, kinds) -> bool:
    return kinds is None or node["kind"] in kinds


def _node_selected(node: dict, path, kinds) -> bool:
    return _path_selected(node, path) and _kind_selected(node, kinds)


class _Graph:
    generation_id = "generation-24"

    def __init__(self):
        self.nodes = {
            "symbol": {"node_id": "symbol", "kind": "function", "identity_key": "alpha", "metadata": {"name": "alpha", "path": "alpha.py"}},
            "caller": {"node_id": "caller", "kind": "function", "identity_key": "call_alpha", "metadata": {"name": "call_alpha", "path": "service.py"}},
            "test": {"node_id": "test", "kind": "function", "identity_key": "test_alpha", "metadata": {"name": "test_alpha", "path": "tests/test_alpha.py"}},
            "module": {"node_id": "module", "kind": "module", "identity_key": "alpha", "metadata": {"name": "alpha", "path": "alpha.py"}},
            "importer": {"node_id": "importer", "kind": "module", "identity_key": "tests.test_import", "metadata": {"name": "tests.test_import", "path": "tests/test_import.py"}},
            "decision": {"node_id": "decision", "kind": "decision", "identity_key": "knowledge/notes/alpha.md", "metadata": {"page_type": "decision"}},
            "page": {"node_id": "page", "kind": "knowledge-page", "identity_key": "knowledge/notes/guide.md", "metadata": {}},
            "checkpoint": {"node_id": "checkpoint", "kind": "checkpoint", "identity_key": "project:7", "metadata": {"sequence": 7}},
            "project-file": {"node_id": "project-file", "kind": "file", "identity_key": "project:file-1", "metadata": {"value": "alpha.py"}},
        }
        self._edges = [
            {"assertion_id": "defines", "source_node_id": "module", "target_node_id": "symbol", "edge_type": "DEFINES", "confidence": "high"},
            {"assertion_id": "imports", "source_node_id": "importer", "target_node_id": "module", "edge_type": "IMPORTS", "confidence": "high"},
            {"assertion_id": "call", "source_node_id": "caller", "target_node_id": "symbol", "edge_type": "CALLS", "confidence": "high"},
            {"assertion_id": "test-call", "source_node_id": "test", "target_node_id": "symbol", "edge_type": "CALLS", "confidence": "high"},
            {"assertion_id": "documents", "source_node_id": "decision", "target_node_id": "symbol", "edge_type": "REFERENCES_SYMBOL", "confidence": "high"},
            {"assertion_id": "page-documents", "source_node_id": "page", "target_node_id": "symbol", "edge_type": "REFERENCES_SYMBOL", "confidence": "high"},
            {"assertion_id": "checkpoint-file", "source_node_id": "checkpoint", "target_node_id": "project-file", "edge_type": "CHECKPOINT_CHANGED_FILE", "confidence": "high"},
        ]

    def find_nodes(self, *, path=None, kinds=None, **_options):
        return [node for node in self.nodes.values() if _node_selected(node, path, kinds)]

    # The bytes this generation indexed: the committed `alpha.py` of `_repository`.
    indexed = {"alpha.py": b"def alpha():\n    return 1\n"}

    def source_by_path(self, relative_path, **_options):
        content = self.indexed.get(relative_path)
        return None if content is None else {"relative_path": relative_path, "content": content}

    def occurrences(self, node_id, **_options):
        if node_id == "symbol":
            return [{"relative_path": "alpha.py", "byte_start": 0, "byte_end": 29, "line_start": 1, "line_end": 2}]
        return []

    def edges(self, **_options):
        return list(self._edges)

    def node(self, node_id):
        return self.nodes.get(node_id)

    def evidence(self, *, assertion_id, **_options):
        return [{"relative_path": "evidence/" + assertion_id, "line_start": 1, "line_end": 1}]

    def close(self):
        pass
