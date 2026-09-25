"""What failing to read a Python file means, in one place.

`ast.parse` raises `SyntaxError` for bad syntax, `ValueError` for a null byte,
and `RecursionError` (the compiler's stack) or `MemoryError` for a very long or
deeply nested expression; a recursive `ast.NodeVisitor` raises `RecursionError`
on a tree a few thousand levels deep. The readers of repository code caught
different subsets, and one generated file froze a repository's code index (audit
A-14, docs/research/2026-09-25-one-hard-python-file-does-not-freeze-the-index.md).
"""

from __future__ import annotations

PARSE_FAILURES: tuple[type[BaseException], ...] = (
    SyntaxError,
    ValueError,
    UnicodeError,
    RecursionError,
    MemoryError,
)
