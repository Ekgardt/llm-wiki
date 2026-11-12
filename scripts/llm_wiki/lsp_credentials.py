"""Existing finite credential masking for LSP log text; no I/O or admission policy."""
from __future__ import annotations

import re

from llm_wiki.lsp_text import is_control

_URL_AUTHORITY_TOKEN_BOUNDARIES = frozenset('"<>\\^`{|}')


_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ai)(?<![a-z0-9_.-])(?P<quote>[\"']?)[a-z0-9_.-]*"
    r"(?:api[_.-]?key|authorization|cookie|passwd|password|"
    r"private[_.-]?key|secret|token)"
    r"(?P=quote)\s*[:=]\s*"
)


# RFC 7468 section 2: a key is the base64 between a BEGIN and an END boundary line.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
)


_PRIVATE_KEY_ORPHAN_END = re.compile(r"\A[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")


_PRIVATE_KEY_MARKER = "<redacted private key>"


# Secrets whose own shape names them, with no key written beside them.
_STANDALONE_SECRET = re.compile(
    r"(?ai)(?<![a-z0-9_.-])"
    r"(?:(?P<scheme>bearer|basic)\s+[a-z0-9._~+/-]{8,}={0,2}"
    r"|sk-[a-z0-9-]{16,}"
    r"|gh[pousr]_[a-z0-9]{16,}"
    r"|xox[abposr]-[a-z0-9-]{10,})"
)


def _quoted_value_end(value: str, value_start: int) -> int:
    """The index after the closing quote (or the end of the text)."""
    quote = value[value_start]
    value_end = value_start + 1
    escaped = False
    while value_end < len(value):
        character = value[value_end]
        value_end += 1
        if character == quote and not escaped:
            break
        escaped = character == "\\" and not escaped
    return value_end


def _line_value_end(value: str, value_end: int) -> int:
    while value_end < len(value) and value[value_end] not in "\r\n":
        value_end += 1
    return value_end


def _assignment_value_end(value: str, value_start: int) -> int:
    if value_start < len(value) and value[value_start] in {'"', "'"}:
        return _quoted_value_end(value, value_start)
    return _line_value_end(value, value_start)


def redact_assignments(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in _CREDENTIAL_ASSIGNMENT.finditer(value):
        if match.start() < cursor:
            continue
        pieces.append(value[cursor : match.end()])
        pieces.append("<redacted>")
        cursor = _assignment_value_end(value, match.end())
    pieces.append(value[cursor:])
    return "".join(pieces)


def _standalone_replacement(match: re.Match[str]) -> str:
    """Keep the scheme word that introduced the secret, never the secret."""
    scheme = match.group("scheme")
    if scheme is None:
        return "<redacted>"
    return f"{scheme} <redacted>"


def redact_standalone_secrets(value: str) -> str:
    return _STANDALONE_SECRET.sub(_standalone_replacement, value)


def _scheme_character(character: str) -> bool:
    return character.isascii() and (
        character.isalpha() or character.isdigit() or character in "+.-"
    )


def _scheme_start(value: str, scheme_end: int) -> int:
    scheme_start = scheme_end
    while scheme_start > 0 and _scheme_character(value[scheme_start - 1]):
        scheme_start -= 1
    return scheme_start


def _is_scheme(value: str, scheme_start: int, scheme_end: int) -> bool:
    if scheme_start >= scheme_end:
        return False
    return value[scheme_start].isascii() and value[scheme_start].isalpha()


def _authority_terminator(character: str) -> bool:
    if character in "/?#" or character in _URL_AUTHORITY_TOKEN_BOUNDARIES:
        return True
    return character.isspace() or is_control(character)


def _authority_end(value: str, authority_start: int) -> int:
    authority_end = authority_start
    while authority_end < len(value) and not _authority_terminator(value[authority_end]):
        authority_end += 1
    return authority_end


def _userinfo_span(value: str, scheme_end: int) -> tuple[int, int] | None:
    """(authority start, index of the last `@`) of a URL with userinfo, else None."""
    if not _is_scheme(value, _scheme_start(value, scheme_end), scheme_end):
        return None
    authority_start = scheme_end + 3
    userinfo_end = value.rfind("@", authority_start, _authority_end(value, authority_start))
    if userinfo_end < authority_start:
        return None
    return authority_start, userinfo_end


def redact_url_userinfo(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    search_start = 0
    while True:
        scheme_end = value.find("://", search_start)
        if scheme_end < 0:
            break
        search_start = scheme_end + 3
        span = _userinfo_span(value, scheme_end)
        if span is None or span[0] < cursor:
            continue
        pieces.append(value[cursor : span[0]])
        pieces.append("<redacted>@")
        cursor = span[1] + 1
    pieces.append(value[cursor:])
    return "".join(pieces)


def redact_private_key_blocks(value: str) -> str:
    """Every PEM private key block in multi-line text, whole or cut at either end.

    A per-line redactor sees a key's body as base64 with nothing naming it (audit
    C-37, docs/research/2026-09-25-a-stderr-tail-is-redacted-before-it-is-cut.md).
    An END boundary left after the blocks are gone lost its BEGIN to a cut, so the
    text before it is body.
    """
    whole = _PRIVATE_KEY_BLOCK.sub(_PRIVATE_KEY_MARKER, value)
    return _PRIVATE_KEY_ORPHAN_END.sub(_PRIVATE_KEY_MARKER, whole)


# Preserve the public helper's import and pickle contract.
redact_private_key_blocks.__module__ = "lsp_security"
