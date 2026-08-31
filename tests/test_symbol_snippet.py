"""CODE-02: a symbol name answers with its bounded source block, or a named refusal."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import symbol_snippet  # noqa: E402

SOURCE = '''class Widget:
    def frob(self):
        first = 1
        return first


def frob():
    return "module level"


def other():
    return 2
'''


def _assert_block(item: dict, start: int, fragment: str) -> None:
    assert item["start_line"] == start
    assert fragment in item["source"]
    assert item["truncated"] is False


def test_every_definition_is_returned_with_its_block(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(SOURCE, encoding="utf-8")
    found = symbol_snippet._file_snippets(tmp_path, "mod.py", "frob")
    assert len(found) == 2
    _assert_block(found[0], 2, "return first")
    _assert_block(found[1], 7, "module level")


def test_a_missing_definition_is_a_named_refusal(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(SOURCE, encoding="utf-8")
    found = symbol_snippet._file_snippets(tmp_path, "mod.py", "absent_name")
    assert found == [{"path": "mod.py", "error": "definition line not found"}]


def test_an_unreadable_file_is_a_named_refusal(tmp_path: Path) -> None:
    found = symbol_snippet._file_snippets(tmp_path, "gone.py", "frob")
    assert found == [{"path": "gone.py", "error": "file unreadable or over 1 MiB"}]


def test_a_long_block_is_cut_and_says_so(tmp_path: Path) -> None:
    body = "def long_one():\n" + "\n".join(
        f"    x{index} = {index}" for index in range(300)
    )
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    found = symbol_snippet._file_snippets(tmp_path, "big.py", "long_one")
    assert found[0]["truncated"] is True
    assert found[0]["end_line"] - found[0]["start_line"] + 1 <= 121
