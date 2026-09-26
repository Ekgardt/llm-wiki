"""What failing to read a Python file means, in one place.

`ast.parse` raises `SyntaxError` for bad syntax, `ValueError` for a null byte,
and `RecursionError` (the compiler's stack) or `MemoryError` for a very long or
deeply nested expression; a recursive `ast.NodeVisitor` raises `RecursionError`
on a tree a few thousand levels deep. The readers of repository code caught
different subsets, and one generated file froze a repository's code index (audit
A-14, docs/research/2026-09-25-one-hard-python-file-does-not-freeze-the-index.md).
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize

PARSE_FAILURES: tuple[type[BaseException], ...] = (
    SyntaxError,
    ValueError,
    UnicodeError,
    RecursionError,
    MemoryError,
)


# Python 3.11+ refuses a chain this long with RecursionError; 3.10 has no such
# guard and a 1 MiB stack (the Windows main thread) dies at about 16 000 terms
# (docs/research/2026-09-26-python-3-10-does-not-crash-on-a-deep-expression.md).
MAX_OPERATOR_CHAIN = 3000
_UNGUARDED = sys.version_info >= (3, 11)
_NON_NESTING = frozenset({",", ":", ".", "=", "(", ")", "[", "]", "{", "}", ";", "->", "@"})
_NESTING_KEYWORDS = frozenset({"and", "or", "not", "if", "else", "in", "is", "lambda"})
_OPERATOR_OR_KEYWORD = re.compile(rb"[-+*/%&|^<>~!]|\b(?:and|or|not|if|else|in|is|lambda)\b")


def parse_python(source: str | bytes, filename: str = "<unknown>") -> ast.Module:
    """`ast.parse`, except that Python 3.10 never gets a chain deep enough to kill it."""
    if not _UNGUARDED:
        _refuse_deep_operator_chain(source)
    return ast.parse(source, filename=filename)


def _refuse_deep_operator_chain(source: str | bytes) -> None:
    data = source.encode("utf-8") if isinstance(source, str) else source
    # Fewer operator characters in the whole file than the limit: no line can pass
    # it, and the tokenize pass (which doubled parse time) is skipped.
    if len(_OPERATOR_OR_KEYWORD.findall(data)) <= MAX_OPERATOR_CHAIN:
        return
    if _longest_operator_chain(data) > MAX_OPERATOR_CHAIN:
        raise RecursionError("expression too deep to parse safely on Python 3.10")


def _longest_operator_chain(data: bytes) -> int:
    """The most nesting operators any one logical line holds; 0 when it does not tokenize."""
    try:
        return _count_per_line(tokenize.tokenize(io.BytesIO(data).readline))
    except (tokenize.TokenError, SyntaxError, UnicodeError, ValueError):
        return 0


def _count_per_line(tokens) -> int:
    longest = current = 0
    for token in tokens:
        if token.type == tokenize.NEWLINE:
            longest, current = max(longest, current), 0
            continue
        current += _nests(token)
    return max(longest, current)


def _nests(token: tokenize.TokenInfo) -> int:
    if token.type == tokenize.OP:
        return int(token.string not in _NON_NESTING)
    return int(token.type == tokenize.NAME and token.string in _NESTING_KEYWORDS)
