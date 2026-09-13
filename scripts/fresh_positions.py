"""Lines read from the file, when the file has moved on without the index.

A generation is rebuilt by the nightly pass, so a definition edited today keeps
answering with yesterday's line until then: measured 2026-09-13, `_page_diverse`
moved from 3030 to 3054 and the answer still said 3030 while the other tool
already said 3054, because its index watches the tree.

Rebuilding the generation on the answer path is not the fix — it is a write, it
took 46 s, and the maintenance fence refuses it during a live session. The fix is
the one current practice names: verify on demand, per file, and never write. One
file is parsed, its definitions are read, and a row whose line moved is corrected
in the answer alone. Research:
`docs/research/2026-09-13-a-shorter-answer-and-a-fresher-line.md`.
"""
from __future__ import annotations

import ast
from pathlib import Path

MAX_FILES = 20
MAX_FILE_BYTES = 4 * 1024 * 1024
_PATH_KEYS = ("file", "relative_path", "path")
_NAME_KEYS = ("qualified_name", "name")
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _remember(lines: dict[str, int], name: str, line: int) -> None:
    lines.setdefault(name, line)


def _record_definition(node: ast.stmt, prefix: str, lines: dict[str, int]) -> None:
    name = str(getattr(node, "name", ""))
    qualified = f"{prefix}{name}"
    _remember(lines, qualified, node.lineno)
    _remember(lines, name, node.lineno)
    _record_body(node, f"{qualified}.", lines)


def _assigned_names(node: ast.stmt) -> list[str]:
    targets = list(getattr(node, "targets", ()) or ())
    single = getattr(node, "target", None)
    if single is not None:
        targets.append(single)
    return [str(target.id) for target in targets if isinstance(target, ast.Name)]


def _record_assignment(node: ast.stmt, prefix: str, lines: dict[str, int]) -> None:
    for name in _assigned_names(node):
        _remember(lines, f"{prefix}{name}", node.lineno)
        _remember(lines, name, node.lineno)


def _record_child(child: ast.stmt, prefix: str, lines: dict[str, int]) -> None:
    if isinstance(child, _DEFINITIONS):
        _record_definition(child, prefix, lines)
        return
    if isinstance(child, (ast.Assign, ast.AnnAssign)):
        _record_assignment(child, prefix, lines)


def _record_body(node: ast.AST, prefix: str, lines: dict[str, int]) -> None:
    for child in getattr(node, "body", ()) or ():
        _record_child(child, prefix, lines)


def _parsed_definitions(text: str) -> dict[str, int]:
    lines: dict[str, int] = {}
    _record_body(ast.parse(text), "", lines)
    return lines


def _record_span(node: ast.stmt, prefix: str, spans: dict[str, tuple[int, int]]) -> None:
    name = str(getattr(node, "name", ""))
    qualified = f"{prefix}{name}"
    end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
    spans.setdefault(qualified, (node.lineno, end))
    spans.setdefault(name, (node.lineno, end))
    _record_spans_body(node, f"{qualified}.", spans)


def _record_spans_body(node: ast.AST, prefix: str, spans: dict) -> None:
    for child in getattr(node, "body", ()) or ():
        if isinstance(child, _DEFINITIONS):
            _record_span(child, prefix, spans)


def definition_spans(path: Path) -> dict[str, tuple[int, int]]:
    """Every class or function in one file, by name, to the lines it spans now."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        spans: dict[str, tuple[int, int]] = {}
        _record_spans_body(ast.parse(path.read_text(encoding="utf-8")), "", spans)
        return spans
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
        return {}


def span_of(path: Path, name: str) -> tuple[int, int] | None:
    """The lines `name` spans in this file now, or None when it is not there."""
    spans = definition_spans(path)
    for candidate in _tail_names(name):
        if candidate in spans:
            return spans[candidate]
    return None


def definition_lines(path: Path) -> dict[str, int]:
    """Every definition in one Python file, by name, to the line it is on now.

    An unreadable, oversized or unparsable file has no definitions here: the
    answer then keeps the line the index recorded, which is the failure mode this
    is allowed to have.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        return _parsed_definitions(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
        return {}


def _row_value(row: dict, keys) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolved(root: Path, candidate: str) -> Path:
    path = Path(candidate)
    return path if path.is_absolute() else Path(root) / path


def _tail_names(name: str) -> list[str]:
    """The name as written, then its last two parts, then its last one."""
    parts = name.split(".")
    return [".".join(parts[index:]) for index in range(len(parts))]


def _line_on_disk(lines: dict[str, int], name: str) -> int | None:
    for candidate in _tail_names(name):
        if candidate in lines:
            return lines[candidate]
    return None


def _corrected(row: dict, lines: dict[str, int]) -> dict:
    name = _row_value(row, _NAME_KEYS)
    found = None if name is None else _line_on_disk(lines, name)
    if found is None or found == row.get("line"):
        return row
    return {**row, "line": found, "line_read_from": "file"}


class _Files:
    """One parse per file, at most `MAX_FILES` of them for one answer."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.seen: dict[str, dict[str, int]] = {}

    def lines(self, candidate: str) -> dict[str, int]:
        if candidate in self.seen:
            return self.seen[candidate]
        if len(self.seen) >= MAX_FILES:
            return {}
        self.seen[candidate] = definition_lines(_resolved(self.root, candidate))
        return self.seen[candidate]


def _refreshed_row(row: object, files: _Files) -> object:
    if not isinstance(row, dict):
        return row
    candidate = _row_value(row, _PATH_KEYS)
    if candidate is None:
        return row
    return _corrected(row, files.lines(candidate))


def refreshed_rows(rows: object, root: Path) -> object:
    """Every row's line, as the file has it now; anything else untouched."""
    if not isinstance(rows, list):
        return rows
    files = _Files(root)
    return [_refreshed_row(row, files) for row in rows]


def refreshed_answer(answer: object, root: Path, keys) -> object:
    """The same answer with `keys`' rows carrying the lines on disk."""
    if not isinstance(answer, dict):
        return answer
    return {
        key: refreshed_rows(value, root) if key in keys else value
        for key, value in answer.items()
    }
