"""Audit 3, B23: a `def` inside an `if`, a `try`, a `with` or a `for` is extracted.

An `if` statement is not a block of the language, so a definition under one
binds its name in the enclosing scope and belongs to the enclosing owner.
Research:
`docs/research/2026-09-17-graph-a-definition-under-a-platform-test-is-still-a-definition.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from code_extractor import extract_code  # noqa: E402
from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord  # noqa: E402

PLATFORM_MODULE = (
    b"import sys\n"
    b"\n"
    b"if sys.platform == 'win32':\n"
    b"    LIMIT = 5\n"
    b"    def wait(handle):\n"
    b"        return handle\n"
    b"else:\n"
    b"    def sleep(handle):\n"
    b"        return 0\n"
    b"\n"
    b"try:\n"
    b"    def parse(text):\n"
    b"        return text\n"
    b"except ValueError:\n"
    b"    PARSE_ERROR = 1\n"
    b"\n"
    b"with sys.stdin as handle:\n"
    b"    class Holder:\n"
    b"        def keep(self):\n"
    b"            return 2\n"
    b"\n"
    b"for name in ('a',):\n"
    b"    def looped():\n"
    b"        return name\n"
    b"\n"
    b"def caller(text):\n"
    b"    return parse(text)\n"
)


def _source(path: str, content: bytes) -> CapturedSource:
    return CapturedSource(
        SourceRecord(
            logical_id=f"source:{path}",
            relative_path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="text/x-python",
            language="python",
            git_oid=None,
        ),
        SourceMetadata(type="code", language="python"),
        content,
    )


def _names(result, kind: str) -> list[str]:
    return sorted(
        node["metadata"]["name"] for node in result.nodes if node["kind"] == kind
    )


def _call_targets(result) -> set[str]:
    by_id = {node["node_id"]: node["metadata"].get("name") for node in result.nodes}
    return {
        by_id[item["target_node_id"]]
        for item in result.assertions
        if item["edge_type"] == "CALLS"
    }


def _extracted():
    return extract_code((_source("plat.py", PLATFORM_MODULE),), repository_id="repo")


def test_definitions_under_if_try_with_and_for_become_nodes():
    result = _extracted()

    assert _names(result, "function") == ["caller", "looped", "parse", "sleep", "wait"]
    assert _names(result, "class") == ["Holder"]
    assert _names(result, "method") == ["keep"]
    assert _names(result, "constant") == ["LIMIT", "PARSE_ERROR"]


def test_a_call_to_a_definition_inside_a_try_block_resolves():
    result = _extracted()
    unnamed = [
        item["target_text"]
        for item in result.observations
        if item["edge_type"] == "CALLS"
    ]

    assert "parse" in _call_targets(result)
    assert unnamed == []
