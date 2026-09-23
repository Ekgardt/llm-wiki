"""A limit defined in two modules is either one shared bound or two named ones.

On 2026-09-23 `scripts/` held 657 module-level numeric limits; 36 names were defined in
more than one module, 15 of them with the same value in every copy, and two of the
same-name pairs bounded the same file with different numbers. See
`docs/research/2026-09-23-one-limit-one-place.md`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
LIMIT = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]{2,})(?:\s*:\s*[^=]+)?\s*=\s*"
    r"(?P<value>-?\d[\d_]*(?:\.\d+)?(?:\s*\*\s*\d[\d_]*)*)\s*(?:#.*)?$"
)
COMMENT_LINES_ABOVE = 3


class Limit(NamedTuple):
    module: str
    name: str
    value: float
    commented: bool


def _value(expression: str) -> float:
    return float(eval(expression.replace("_", ""), {"__builtins__": {}}))  # noqa: S307 - digits and `*`


def _limit_at(path: Path, lines: list[str], index: int) -> Limit | None:
    match = LIMIT.match(lines[index])
    if match is None:
        return None
    window = "\n".join(lines[max(0, index - COMMENT_LINES_ABOVE):index + 1])
    return Limit(path.name, match.group("name"), _value(match.group("value")), "#" in window)


def _limits(path: Path) -> list[Limit]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found = [_limit_at(path, lines, index) for index in range(len(lines))]
    return [limit for limit in found if limit is not None]


def _all_limits() -> list[Limit]:
    return [limit for path in sorted(SCRIPTS.glob("*.py")) for limit in _limits(path)]


def _grouped(limits: list[Limit]) -> dict[str, list[Limit]]:
    grouped: dict[str, list[Limit]] = defaultdict(list)
    for limit in limits:
        grouped[limit.name].append(limit)
    return grouped


def _shared_names(grouped: dict[str, list[Limit]]) -> dict[str, list[Limit]]:
    return {name: rows for name, rows in grouped.items() if len({row.module for row in rows}) > 1}


def _one_value(rows: list[Limit]) -> bool:
    return len({row.value for row in rows}) == 1


def test_no_limit_is_copied_with_the_same_value_into_a_second_module() -> None:
    shared = _shared_names(_grouped(_all_limits()))
    copied = {name: sorted(row.module for row in rows) for name, rows in shared.items() if _one_value(rows)}
    assert copied == {}, f"share one definition instead of copying it: {copied}"


def _silent(rows: list[Limit]) -> list[str]:
    return [f"{row.module}:{row.name}" for row in rows if not row.commented]


def test_a_name_reused_for_a_different_bound_says_what_it_bounds() -> None:
    shared = _shared_names(_grouped(_all_limits()))
    silent = sorted(item for name, rows in shared.items() if not _one_value(rows) for item in _silent(rows))
    assert silent == [], f"a comment within {COMMENT_LINES_ABOVE} lines must say what this bound is: {silent}"
