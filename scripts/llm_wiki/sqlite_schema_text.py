"""Conservative lexical comparison of managed SQLite schema declarations."""
from __future__ import annotations

import re

_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')
_TOKENS = re.compile(
    r"(?P<protected>'(?:''|[^'])*(?:'|\Z)"
    r'|"(?:""|[^"])*(?:"|\Z)'
    r'|`(?:``|[^`])*(?:`|\Z)'
    r'|\[[^\]]*(?:\]|\Z)'
    r'|--[^\r\n]*(?:\r\n|\r|\n|\Z)'
    r'|/\*[\s\S]*?(?:\*/|\Z))'
    r'|(?P<space>[ \t\r\n\f]+)'
    r"|(?P<plain>[^'\"`\[\s/\-]+|.)",
    re.DOTALL,
)


def _normalized_token(token: re.Match[str]) -> str:
    if token.lastgroup == 'protected':
        return token.group()
    if token.lastgroup == 'space':
        return ' '
    return token.group().translate(_ASCII_LOWER)


def _trim_outside_space(tokens: list[re.Match[str]]) -> None:
    if tokens and tokens[0].lastgroup == 'space':
        del tokens[0]
    if tokens and tokens[-1].lastgroup == 'space':
        tokens.pop()


def normalized_schema_sql(value: str | None) -> str:
    """Preserve protected contents and Unicode; normalize ASCII syntax spacing."""
    tokens = list(_TOKENS.finditer(value or ''))
    _trim_outside_space(tokens)
    return ''.join(map(_normalized_token, tokens))
