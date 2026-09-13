"""A line the file has moved on from is corrected on the answer path.

The generation is rebuilt nightly; an edit made today must not answer with
yesterday's line. Research:
`docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fresh_positions  # noqa: E402


def _module(tmp_path: Path, body: str, name: str = "module.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_definition_that_moved_answers_with_the_line_it_is_on(tmp_path):
    _module(tmp_path, "\n\n\ndef target():\n    return 1\n")
    rows = [{"file": "module.py", "line": 1, "qualified_name": "module.target"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 4
    assert refreshed[0]["line_read_from"] == "file"


def test_a_line_that_still_holds_is_left_exactly_as_it_was(tmp_path):
    _module(tmp_path, "def target():\n    return 1\n")
    rows = [{"file": "module.py", "line": 1, "qualified_name": "module.target"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0] == rows[0]


def test_a_symbol_that_left_the_file_keeps_the_stored_line(tmp_path):
    """Nothing is invented: an absent symbol is not renumbered."""
    _module(tmp_path, "def other():\n    return 1\n")
    rows = [{"file": "module.py", "line": 11, "qualified_name": "module.target"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 11


def test_a_method_is_found_under_its_class(tmp_path):
    _module(tmp_path, "\nclass Holder:\n\n    def member(self):\n        return 1\n")
    rows = [{"file": "module.py", "line": 1, "qualified_name": "module.Holder.member"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 4


def test_a_module_constant_is_found_too(tmp_path):
    _module(tmp_path, "\n\nEDITORIAL_NAMES = frozenset({'a'})\n")
    rows = [{"file": "module.py", "line": 1, "qualified_name": "EDITORIAL_NAMES"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 3


def test_a_file_that_cannot_be_parsed_changes_nothing(tmp_path):
    _module(tmp_path, "def broken(:\n")
    rows = [{"file": "module.py", "line": 7, "qualified_name": "module.broken"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 7


def test_a_missing_file_changes_nothing(tmp_path):
    rows = [{"file": "absent.py", "line": 7, "qualified_name": "absent.target"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed[0]["line"] == 7


def test_a_row_without_a_path_is_untouched(tmp_path):
    rows = [{"line": 7, "qualified_name": "module.target"}]

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert refreshed == rows


def test_one_file_is_parsed_once_for_every_row_of_it(tmp_path, monkeypatch):
    _module(tmp_path, "\n\ndef target():\n    return 1\n")
    parses = []
    original = fresh_positions.definition_lines

    def counted(path):
        parses.append(path)
        return original(path)

    monkeypatch.setattr(fresh_positions, "definition_lines", counted)
    rows = [
        {"file": "module.py", "line": 1, "qualified_name": "module.target"},
        {"file": "module.py", "line": 2, "qualified_name": "module.target"},
    ]

    fresh_positions.refreshed_rows(rows, tmp_path)

    assert len(parses) == 1


def _four_modules(tmp_path: Path) -> list[dict]:
    for index in range(4):
        _module(tmp_path, "\n\ndef target():\n    return 1\n", f"m{index}.py")
    return [
        {"file": f"m{index}.py", "line": 1, "qualified_name": "target"}
        for index in range(4)
    ]


def _corrected_count(refreshed: object) -> int:
    return len([row for row in refreshed if row["line"] == 3])


def test_no_more_files_than_the_cap_are_read(tmp_path, monkeypatch):
    monkeypatch.setattr(fresh_positions, "MAX_FILES", 2)
    rows = _four_modules(tmp_path)

    refreshed = fresh_positions.refreshed_rows(rows, tmp_path)

    assert _corrected_count(refreshed) == 2


def test_only_the_named_keys_of_an_answer_are_refreshed(tmp_path):
    _module(tmp_path, "\n\ndef target():\n    return 1\n")
    row = {"file": "module.py", "line": 1, "qualified_name": "target"}
    answer = {"callers": [dict(row)], "community": [dict(row)]}

    refreshed = fresh_positions.refreshed_answer(answer, tmp_path, ("callers",))

    assert refreshed["callers"][0]["line"] == 3
    assert refreshed["community"][0]["line"] == 1


def test_a_span_covers_the_whole_definition(tmp_path):
    _module(tmp_path, "\ndef target():\n    a = 1\n    return a\n")

    span = fresh_positions.span_of(tmp_path / "module.py", "module.target")

    assert span == (2, 4)


def test_a_span_for_an_absent_name_is_nothing(tmp_path):
    _module(tmp_path, "def other():\n    return 1\n")

    assert fresh_positions.span_of(tmp_path / "module.py", "target") is None
