"""Shared secret redaction for durable daily-log capture.

Pattern set informed by gitleaks v8.30.1 (per-rule Shannon entropy) and
TruffleHog v3.95.9 (800+ detectors with active verification). This module
is a best-effort real-time redactor for hook-level ms-latency usage — it
is NOT a full DLP scanner. For CI secret scanning, rely on gitleaks.
"""
from __future__ import annotations

import math
import re

# A credential-named key followed by a value. The name alone decides nothing:
# `lease_token: str` is a type annotation, `token = next(iterator)` is an
# expression, `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` is a reference to a
# secret rather than a secret. Only `_value_is_credential` below decides, and
# it looks at the value.
# The separator may not cross a line. `.env`, YAML, JSON, HTTP headers and
# source assignments all put the value beside the key; a name and a colon at
# the end of a line opens a block, as in `class CancellationToken:`, and what
# follows is the block, not a value. A YAML scalar indented onto the next line
# is the price, and it is named here rather than silently paid.
_SAME_LINE = r"[^\S\r\n]*"
_NAMED_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"(?i)({name}{_SAME_LINE}{separator}{_SAME_LINE})(\S+)")
    for name, separator in (
        (r"authorization", r":[^\S\r\n]*bearer[^\S\r\n]"),
        (r"api[_-]?key", r"[=:]"),
        (r"secret", r"[=:]"),
        (r"password", r"[=:]"),
        (r"token", r"[=:]"),
        (r"entropy", r"[=:]"),
    )
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # A provider key starts a token. Without this guard `sk-` matched inside
    # `dead-task-retirement-and-restore-decision`, the fail-closed DLP boundary
    # quarantined the write, and this vault could publish no knowledge at all.
    # Punctuation is still a boundary, so `KEY=sk-…`, `"sk-…"` and `(sk-…)` are
    # caught as before. See docs/research/2026-08-22-secret-prefix-boundaries.md.
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{18,}"), "[REDACTED_API_KEY]"),
    # GitHub ships six prefixes, not one, and the fine-grained tokens carry a
    # seventh shape. See docs/research/2026-08-25-which-secret-shapes-are-worth-a-pattern.md.
    (
        re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # Underscore keys (Stripe and everyone who copied the shape). The existing
    # `sk-` rule never saw these, and the prefix does not name the vendor, so
    # the replacement does not claim one.
    (
        re.compile(r"(?<![A-Za-z0-9])[sr]k_(live|test)_[A-Za-z0-9]{16,}"),
        "[REDACTED_API_KEY]",
    ),
    (re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{30,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])GOCSPX-[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])xapp-[0-9]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PEM_KEY]"),
]

_HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"
)
_PURE_HEX_RE = re.compile(r"^[0-9a-f]+$")
_ENTROPY_THRESHOLD = 4.0
_MIN_BASE64_SEGMENT = 3
_MIN_BASE64_RUN = 16

_QUOTE_CHARACTERS = "\"'`"
# Syntax a credential literal never contains: calls, subscripts, generics,
# SQL placeholders, shell and CI interpolation, and escapes. Base64 padding is
# a trailing `=`, so `=` stays legal.
_CODE_CHARACTERS = frozenset("()[]{}<>$\\?*|&")
# A comma or semicolon ends the value and starts the next field, in
# `connect(token="…",timeout=5)` as in `SET lease_token=NULL,lease_expires_at=NULL`.
_VALUE_END_RE = re.compile(r"[,;]")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Below this a value is indistinguishable from a keyword, a type name, or a
# small integer, and the false refusal costs more than the missed short secret.
# This bounds only the key/value rules; the entropy rule keeps its own floor.
_MIN_CREDENTIAL_VALUE_CHARS = 8


def _shannon_entropy(data: str) -> float:
    if not data:
        return 0.0
    freq: dict[str, int] = {}
    for c in data:
        freq[c] = freq.get(c, 0) + 1
    n = len(data)
    return -sum((f / n) * math.log2(f / n) for f in freq.values())


def _redact_patterns(text: str) -> str:
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _unquote(value: str) -> tuple[bool, str]:
    """Strip surrounding quotes and say whether there were any."""
    stripped = value.strip(_QUOTE_CHARACTERS)
    return stripped != value, stripped


def _value_is_code(value: str) -> bool:
    """`next(iterator)`, `tuple[bytes,`, `${{ secrets.X }}`, `?`, `128`."""
    return bool(_CODE_CHARACTERS & set(value)) or value.isdigit()


def _is_symbol_reference(value: str) -> bool:
    """`owner_token`, `lease.token`, `NO_CONTRADICTIONS` — a name, not a value.

    Only unquoted values are read this way: source code writes a reference bare
    and a literal in quotes, so a quoted `"my_secret_value"` is still a finding.
    """
    if "_" not in value and "." not in value:
        return False
    return all(_IDENTIFIER_RE.fullmatch(part) for part in value.split("."))


def _matches_known_secret_shape(value: str) -> bool:
    """A vendor prefix outranks the identifier shape.

    `ghp_abcdefghijklmnopqrstuvwxyz012345` is `[A-Za-z_][A-Za-z0-9_]*` — exactly
    an identifier — and so are `github_pat_…`, `npm_…` and `hf_…`. Without this
    the reference rule would hand them to the prefix pass, which redacts them
    under a different marker; downstream code asserts, hashes and stores that
    marker.
    """
    return any(pattern.search(value) for pattern, _replacement in _PATTERNS)


def _bare_value_is_credential(value: str) -> bool:
    if _matches_known_secret_shape(value):
        return True
    return not value.isalpha() and not _is_symbol_reference(value)


def _value_is_credential(raw: str) -> bool:
    """Whether the value after a credential-named key is a credential.

    The key names a slot; it does not prove the slot holds a secret. Declaring
    the type of the slot, assigning an expression to it, or pointing at a
    secret stored elsewhere all leave the secret itself absent.
    """
    quoted, value = _unquote(_VALUE_END_RE.split(raw, maxsplit=1)[0])
    if len(value) < _MIN_CREDENTIAL_VALUE_CHARS or _value_is_code(value):
        return False
    return True if quoted else _bare_value_is_credential(value)


def _replace_named_value(match: re.Match[str]) -> str:
    if _value_is_credential(match.group(2)):
        return f"{match.group(1)}[REDACTED]"
    return match.group(0)


def _redact_named_values(text: str) -> str:
    out = text
    for pattern in _NAMED_VALUE_PATTERNS:
        out = pattern.sub(_replace_named_value, out)
    return out


def _looks_like_path(token: str) -> bool:
    """Slash runs whose entropy comes from joining words are paths, not blobs.

    macOS temporary directories look exactly like base64 to an entropy test:
    `5/zjnzxgh147qcg3bb5cg2wvqw0000gn/T/pytest` is 41 characters of
    `[A-Za-z0-9/]` at entropy 4.46. Redacting it corrupts every stored path and
    every log line that mentions one. A real blob does not contain one- or
    two-character slash-separated pieces, and it carries at least one long
    dense run between separators.

    The second rule below covers the case the first misses: a URL path whose
    only dense piece is a digest, as in
    `gist.github.com/karpathy/442a6bf555914893e9891c11519de94f`. A bare digest
    is deliberately exempt (`_PURE_HEX_RE`); the same digest reached through a
    path must be exempt too, and the surrounding words must not be what makes
    it look random. `/` is a base64 character as well as a separator, so the
    question is settled by the pieces: a blob keeps its randomness inside one
    separator-free run, a path spreads it across several meaningful ones.
    """
    if "/" not in token:
        return False
    segments = _token_segments(token)
    if _segment_shape_is_path(segments):
        return True
    return not any(_segment_is_blob(segment) for segment in segments)


def _segment_is_blob(segment: str) -> bool:
    """One separator-free run that is opaque on its own.

    An all-letter run is a word: base64 draws from 64 symbols, so sixteen
    consecutive characters without a single digit are a name, not a payload.
    Measured need: `CreatingLaunchdJobs` in an Apple documentation URL scores
    4.04, just over the entropy threshold, and is plainly not a secret.
    """
    return (
        len(segment) >= _MIN_BASE64_RUN
        and not segment.isalpha()
        and _PURE_HEX_RE.match(segment) is None
        and _shannon_entropy(segment) >= _ENTROPY_THRESHOLD
    )


def _token_segments(token: str) -> list[str]:
    return [segment for segment in token.split("/") if segment]


def _segment_shape_is_path(segments: list[str]) -> bool:
    if not segments:
        return True
    if min(len(segment) for segment in segments) < _MIN_BASE64_SEGMENT:
        return True
    return max(len(segment) for segment in segments) < _MIN_BASE64_RUN


def _is_high_entropy(token: str) -> bool:
    if _PURE_HEX_RE.match(token):
        return False
    if _looks_like_path(token):
        return False
    return _shannon_entropy(token) >= _ENTROPY_THRESHOLD


def _redact_high_entropy(text: str) -> str:
    out = text
    for match in _HIGH_ENTROPY_RE.finditer(text):
        if _is_high_entropy(match.group()):
            out = out.replace(match.group(), "[REDACTED_TOKEN]")
    return out


def redact_secrets(text: str) -> str:
    """Return text with common secret patterns replaced."""
    if not text or not isinstance(text, str):
        return text
    # Order is load-bearing: the key/value rules were the first six entries of
    # `_PATTERNS`, so `token=sk-…` collapsed to `token=[REDACTED]` and never to
    # `token=[REDACTED_API_KEY]`. Splitting them into their own pass must not
    # renumber that — the marker is asserted, hashed and stored downstream.
    return _redact_high_entropy(_redact_patterns(_redact_named_values(text)))
