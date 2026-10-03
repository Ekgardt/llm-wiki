"""A source block includes its final behavior, even past the old display cut."""

from __future__ import annotations

import ast
from functools import partial
from pathlib import Path

import pytest
import symbol_snippet


def _source(line_count: int) -> str:
    body = ["def target():"]
    body.extend(f"    value_{number} = {number}" for number in range(line_count - 2))
    body.append("    return 'required final behavior'")
    return "\n".join(body)


def _stored(source: str) -> dict:
    node = ast.parse(source).body[0]
    return symbol_snippet._exact_block(
        source.splitlines(), {"line_start": node.lineno, "line_end": node.end_lineno}
    )


def _heuristic(root: Path, source: str) -> dict:
    (root / "module.py").write_bytes(source.encode())
    [block] = symbol_snippet._file_snippets(root, "module.py", "target")
    return block


def _fresh_file(root: Path, source: str) -> dict:
    (root / "module.py").write_bytes(source.encode())
    node = {
        "kind": "function", "identity_key": "target", "node_id": "target-node",
        "metadata": {"name": "target"},
    }
    return symbol_snippet._block_for(
        root, "module.py", ["def target():", "    return 'old'"],
        {"line_start": 1, "line_end": 2}, node, "stale",
    )


@pytest.mark.parametrize("line_count", [120, 121, 301])
@pytest.mark.parametrize("reader", ["stored", "heuristic", "fresh_file"])
def test_all_readers_keep_the_last_behavior(tmp_path: Path, line_count: int, reader: str):
    source = _source(line_count)
    readers = {
        "stored": _stored,
        "heuristic": partial(_heuristic, tmp_path),
        "fresh_file": partial(_fresh_file, tmp_path),
    }
    block = readers[reader](source)
    assert block["source"] == source
    assert (block["start_line"], block["end_line"]) == (1, line_count)
    assert block["truncated"] is False


def test_complete_definition_survives_the_mcp_answer_budget():
    from mcp_server import _shaped_code_answer

    source = _source(301)
    block = {"path": "module.py", **_stored(source)}
    shaped = _shaped_code_answer({"symbol": "target", "snippets": [block]}, {})
    assert shaped["snippets"][0]["source"] == source
    assert shaped["snippets"][0]["truncated"] is False
