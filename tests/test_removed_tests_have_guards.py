"""Every guard the removed-test record names is a test that still exists.

`docs/research/2026-11-17-where-each-removed-test-is-guarded.md` says where the
behaviour of each test this branch removed is now checked. A guard that was renamed
or deleted later would leave that record pointing at nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / "docs" / "research" / "2026-11-17-where-each-removed-test-is-guarded.md"
ROW = re.compile(r"^\| `(?P<removed>[^`]+)` \| [^|]+ \| [^|]+ \| (?P<guards>.+) \|$", re.M)
GUARD = re.compile(r"`(tests/[^`:]+\.py)(?:::([^`]+))?`")


def _rows() -> list[re.Match[str]]:
    return list(ROW.finditer(RECORD.read_text(encoding="utf-8")))


def _defines(path: str, test: str | None) -> bool:
    source = ROOT / path
    if not source.is_file():
        return False
    if test is None:
        return True
    name = test.split("::")[-1]
    return re.search(rf"^\s*def {re.escape(name)}\(", source.read_text(encoding="utf-8"), re.M) is not None


def _named_guards() -> list[tuple[str, str]]:
    return [guard for row in _rows() for guard in GUARD.findall(row["guards"])]


def _missing_guards() -> list[str]:
    return [f"{path}::{test}" for path, test in _named_guards() if not _defines(path, test or None)]


def test_the_record_lists_removed_tests() -> None:
    assert len(_rows()) > 0


def test_every_named_guard_exists() -> None:
    assert _missing_guards() == []


def test_a_removed_test_is_not_also_still_present() -> None:
    present = [row["removed"] for row in _rows() if _defines(*row["removed"].split("::", 1))]
    assert present == []
