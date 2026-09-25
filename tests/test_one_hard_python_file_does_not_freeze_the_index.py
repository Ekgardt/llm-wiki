"""A Python file too deep to parse is one file's parse error, not the index's end.

A generated file with one very long expression raised `RecursionError` from
`ast.parse`, which no reader caught, and the repository's refresh failed on it
every time. See docs/research/2026-09-25-one-hard-python-file-does-not-freeze-the-index.md.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import import_resolver
import path_coverage
from code_extractor import extract_code
from corpus_snapshot import CapturedSource, SourceMetadata, SourceRecord

DEEP = ("x = " + "+".join(["1"] * 100_000) + "\n").encode()


def _deep_parse_fails() -> bool:
    """Python 3.10 parses this expression; 3.11 and later raise `RecursionError`."""
    try:
        ast.parse(DEEP)
    except RecursionError:
        return True
    return False


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


def test_the_deep_file_is_a_parse_error_and_its_neighbour_is_extracted() -> None:
    result = extract_code(
        (_source("generated.py", DEEP), _source("ok.py", b"def fine():\n    return 1\n")),
        repository_id="repo",
    )

    names = {node["metadata"].get("name") for node in result.nodes}
    kinds = {observation.get("reason") for observation in result.observations}
    assert ("fine" in names, "parse_error" in kinds) == (True, _deep_parse_fails())


def test_the_import_reader_and_the_parse_probe_answer_for_it(tmp_path: Path) -> None:
    path = tmp_path / "generated.py"
    path.write_bytes(DEEP)

    assert import_resolver.resolve_python_imports_and_calls(path) == ([], [])
    kinds = [error["kind"] for error in path_coverage._python_parse(DEEP)["errors"]]
    assert kinds == (["RecursionError"] if _deep_parse_fails() else [])
