"""A stored instant's text order is its time order (audit 2026-09-26 C-12).

docs/research/2026-09-26-every-stored-instant-has-one-width.md
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WHOLE = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("module_name", ["iso_time", "markdown_transaction", "project_journal"])
def test_a_whole_second_sorts_before_the_half_second_after_it(module_name) -> None:
    import importlib

    module = importlib.import_module(module_name)
    write = getattr(module, "utc_text", None) or module._timestamp

    earlier, later = write(WHOLE), write(WHOLE + timedelta(milliseconds=500))

    assert (earlier, earlier < later) == ("2026-09-26T10:00:00.000000Z", True)



_SQL_TIME_COMPARISON = re.compile(r"_at\s*(?:<=|>=|<|>)\s*\?")


def _is_calendar_date(node: ast.AST) -> bool:
    """`date.fromisoformat(...)`: a day has no fraction to drop."""
    return ast.unparse(node).startswith("date.fromisoformat(")


def _bare_isoformat(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "isoformat" or node.keywords:
        return False
    return not _is_calendar_date(node.func.value)


def _bare_isoformat_lines(tree: ast.AST) -> list[int]:
    """`x.isoformat()` with no `timespec`, on anything but a calendar date."""
    return [node.lineno for node in ast.walk(tree) if _bare_isoformat(node)]


def test_a_module_that_compares_times_in_sql_writes_them_in_one_width() -> None:
    offenders = {}
    for path in sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _SQL_TIME_COMPARISON.search(source):
            offenders[path.name] = _bare_isoformat_lines(ast.parse(source))

    assert {name: lines for name, lines in offenders.items() if lines} == {}
    assert "markdown_transaction.py" in offenders
