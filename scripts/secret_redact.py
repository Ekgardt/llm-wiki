"""Shared secret redaction for durable daily-log capture.

Pattern set informed by gitleaks v8.30.1 (per-rule Shannon entropy) and
TruffleHog v3.95.9 (800+ detectors with active verification). This module
is a best-effort real-time redactor for hook-level ms-latency usage — it
is NOT a full DLP scanner. For CI secret scanning, rely on gitleaks.
"""
from __future__ import annotations

import json
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
# Any name that contains a credential word, as gitleaks' `generic-api-key` reads
# one: `AWS_SECRET_ACCESS_KEY`, `client_secret`, `db.password` — and a JSON key,
# whose closing quote sits between the name and the separator. See
# docs/research/2026-09-25-a-secret-in-json-is-still-a-secret.md.
#
# The credential word must END the key (a digit or separator suffix aside):
# `tokenizer_name`, `token_url`, `secretName` and `password_reset_url` name
# something else, and `PWD`/`OLDPWD` are the shell's directories. See
# docs/research/2026-09-26-a-credential-is-named-at-the-end-of-its-key.md.
_CREDENTIAL_NAME = (
    r"(?<![\w.-])(?!(?:old)?pwd(?![\w.-]))[\w.-]{0,50}?"
    r"(?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credentials?|entropy)[_.-]?\d*(?![\w.-])"
)
# A quote, or a quote escaped inside a JSON string (`\"`).
_QUOTE = r"\\?[\"']"
# A quoted value whole (spaces included) or a bare one that stops before a
# backslash, so an escaped closing quote of a JSON string is never swallowed.
_VALUE = r"(\\?\"[^\"\r\n\\]*\\?\"|'[^'\r\n]*'|[^\s\\]+)"
# Candidate starts are a superset; the original pattern still decides every match.
_CREDENTIAL_VALUE_PATTERN = re.compile(
    rf"(?i)({_CREDENTIAL_NAME}(?:{_QUOTE})?{_SAME_LINE}[=:]{_SAME_LINE}){_VALUE}"
)
_ASSIGNMENT_HEAD = re.compile(rf"(?<![\w.-])[\w.-]+(?:{_QUOTE})?{_SAME_LINE}[=:]")
_NAMED_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?i)(authorization(?:{_QUOTE})?{_SAME_LINE}:{_SAME_LINE}(?:{_QUOTE})?"
        rf"(?:bearer|basic|token)[^\S\r\n])([^\s\\\"']+)"
    ),
    _CREDENTIAL_VALUE_PATTERN,
)

_PATTERN_RULES: list[tuple[re.Pattern[str], str, tuple[str, ...]]] = [
    # A provider key starts a token. Without this guard `sk-` matched inside
    # `dead-task-retirement-and-restore-decision`, the fail-closed DLP boundary
    # quarantined the write, and this vault could publish no knowledge at all.
    # Punctuation is still a boundary, so `KEY=sk-…`, `"sk-…"` and `(sk-…)` are
    # caught as before. See docs/research/2026-08-22-secret-prefix-boundaries.md.
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{18,}"), "[REDACTED_API_KEY]", ('sk-',)),
    # GitHub ships six prefixes, not one, and the fine-grained tokens carry a
    # seventh shape. See docs/research/2026-08-25-which-secret-shapes-are-worth-a-pattern.md.
    (
        re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
     ('gh',)),
    (
        re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
     ('github_pat_',)),
    # Underscore keys (Stripe and everyone who copied the shape). The existing
    # `sk-` rule never saw these, and the prefix does not name the vendor, so
    # the replacement does not claim one.
    (
        re.compile(r"(?<![A-Za-z0-9])[sr]k_(live|test)_[A-Za-z0-9]{16,}"),
        "[REDACTED_API_KEY]",
     ('k_',)),
    (re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}"), "[REDACTED_API_KEY]", ('npm_',)),
    (re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), "[REDACTED_API_KEY]", ('hf_',)),
    (re.compile(r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{30,}"), "[REDACTED_API_KEY]", ('pypi-',)),
    (re.compile(r"(?<![A-Za-z0-9])GOCSPX-[A-Za-z0-9_-]{20,}"), "[REDACTED_API_KEY]", ('GOCSPX-',)),
    (re.compile(r"(?<![A-Za-z0-9])xapp-[0-9]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]", ('xapp-',)),
    (re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]", ('xox',)),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]", ('AKIA',)),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]", ('AIza',)),
    (
        re.compile(
            r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
        "[REDACTED_JWT]",
     ('eyJ',)),
    # A key with no END line (`head id_rsa`) is redacted to the end of the text: what
    # follows its BEGIN line is the key until something proves otherwise.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
        "[REDACTED_PEM_KEY]",
     ('PRIVATE KEY-----',)),
    # Google OAuth access tokens, Telegram bot tokens, Slack incoming webhooks
    # (audit 2026-09-27 B-4, docs/research/2026-09-27-the-redactor-knows-the-missing-shapes.md).
    (re.compile(r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_-]{20,}"), "[REDACTED_GOOGLE_TOKEN]", ('ya29.',)),
    # A bot id, a colon and the secret: the Bot API documents `123456:ABC-DEF1234ghIkl-…`
    # (a 34-character secret); issued secrets run 35. From 30 on it is a token, not a clock.
    (re.compile(r"(?<![\w:])\d{6,10}:[A-Za-z0-9_-]{30,}(?![\w-])"), "[REDACTED_TELEGRAM_TOKEN]", ()),
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"), "[REDACTED_SLACK_WEBHOOK]", ('https://hooks.slack.com/services/',)),
    # A credential in a URL query (`?api_key=…`, `&access_token=…`, a signed URL's `sig=`).
    (
        re.compile(
            r"(?i)([?&](?:api[_-]?key|(?:access|refresh|id)[_-]?token|token|client[_-]?secret|secret|"
            r"password|passwd|pwd|auth|sig|signature|key)=)[^&\s#\"'<>]+"
        ),
        r"\1[REDACTED]",
     ('?', '&')),
    (re.compile(r"(?<![A-Za-z0-9])glpat-[\w-]{20,}"), "[REDACTED_GITLAB_TOKEN]", ('glpat-',)),
    # The password in `scheme://user:password@host` (RFC 3986 3.2.1 deprecates it
    # for exactly this reason); the user and the host stay readable.
    # Up to the LAST `@` of the authority (`user:p@ss@host`), never a port alone.
    # A scheme starts where no scheme character precedes it: `\b` let the scheme run
    # start at every dot of `a.a.a…` and rescan it, 6.7 s on 40 KB (audit 2026-09-27 B-6).
    (re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^\s/:@]+:)(?!\d+@)[^\s/]+(@)"), r"\1[REDACTED]\2", ('://',)),
    # `--password=X`, `--password X` (docker login, podman, many CLIs).
    (re.compile(r"(?<![\w-])(--password(?:=|[^\S\r\n]+))(?![$-])[^\s]+"), r"\1[REDACTED]", ('--password',)),
    # MySQL's `-pPASSWORD`, `sshpass -p` and `docker login -p` are redacted by
    # `_redact_command_passwords`, one pass per line (a lazy scan per command name was
    # quadratic: 2.5 s on 40 KB of `mysql `, audit 2026-09-27 B-6).
]

# A prerequisite is certified by the unchanged rule's exact source and flags.
# Unknown or changed rules run their full regex. Case-insensitive rules use
# punctuation only, preserving Python's Unicode case matching.
_PATTERNS = [(pattern, replacement) for pattern, replacement, _ in _PATTERN_RULES]
_PATTERN_PREREQUISITES = {
    (pattern.pattern, pattern.flags): literals
    for pattern, _replacement, literals in _PATTERN_RULES if literals
}


# A command whose `-p` takes a password: MySQL clients only attached (`-pX`; `-p db`
# prompts and names a database), sshpass and docker login attached or separated.
_PASSWORD_COMMAND = re.compile(
    r"(?i)(?<![\w.-])(?:(mysql(?:dump|admin|import|show|check)?)|sshpass|docker[^\S\r\n]+login)(?![\w.-])"
)
# A quoted password is taken whole, spaces and all.
_FLAG_VALUE = r"(?:'[^'\r\n]*'|\"[^\"\r\n]*\"|\S+)"
_ATTACHED_PASSWORD = re.compile(r"(?<!\S)(-p)(?![\s$])" + _FLAG_VALUE)
_ANY_PASSWORD = re.compile(r"(?<!\S)(-p(?:[^\S\r\n]+)?)(?![\s$-])" + _FLAG_VALUE)

# Options belong to their command: `date -u +%H:%M:%S` is not credentials.
# Quoted shell operators remain in the command; unquoted ones end its region.
# See docs/research/2026-09-29-a-user-flag-belongs-to-its-command.md.
_CURL_COMMAND = re.compile(
    r"(?<![\w.-])(?i:curl(?:\.exe)?)[\"']?(?=\s)"
    r"(?:\\\r?\n|`\r?\n|'[^'\r\n]*(?:'|$)|\"[^\"\r\n]*(?:\"|$)|[^'\";&|\r\n])*"
)
_CURL_USER = re.compile(r"(?<![\w-])(?:-u|--user)(?:[^\S\r\n]+|=)" + _FLAG_VALUE)


def _curl_credentials(match: re.Match[str]) -> str:
    value = match.group()
    prefix, colon, password = value.partition(":")
    if not colon or password.startswith("$"):
        return value
    closing = ""
    if password.endswith(("'", '"')):
        closing = password[-1]
    return prefix + ":[REDACTED]" + closing


def _curl_command(match: re.Match[str]) -> str:
    return _CURL_USER.sub(_curl_credentials, match.group())


def _command_line(line: str) -> str:
    command = _PASSWORD_COMMAND.search(line)
    if command is None:
        return line
    flag = _ATTACHED_PASSWORD if command.group(1) else _ANY_PASSWORD
    return line[: command.end()] + flag.sub(r"\1[REDACTED]", line[command.end():])


def _redact_command_passwords(text: str) -> str:
    return "".join(_command_line(line) for line in text.splitlines(keepends=True))

_HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"
)
_PURE_HEX_RE = re.compile(r"^[0-9a-f]+$")
# Shannon bits per character above which an unlabelled run is taken for a secret: random base64 is
# near 6, hex at most 4, prose lower. Basis unknown: value predates measurement; review when a
# real secret or a path is misjudged (see _MIN_BASE64_RUN).
_ENTROPY_THRESHOLD = 4.0
# A slash-separated run whose shortest segment is shorter than this reads as a path, not a key
# (ba81f395: macOS temporary paths were redacted). Basis unknown: value predates measurement;
# review when a key or a path is misjudged.
_MIN_BASE64_SEGMENT = 3
# The longest segment a high-entropy run needs before it is a secret (ba81f395). Basis unknown:
# value predates measurement; review when a key or a path is misjudged.
_MIN_BASE64_RUN = 16

# Syntax a credential literal never contains: calls, subscripts, generics,
# SQL placeholders, shell and CI interpolation, and escapes. Base64 padding is
# a trailing `=`, so `=` stays legal.
_CODE_CHARACTERS = frozenset("()[]{}<>$\\?*|&")
# A comma or semicolon ends the value and starts the next field, in
# `connect(token="…",timeout=5)` as in `SET lease_token=NULL,lease_expires_at=NULL`;
# a closing brace or bracket ends a JSON value.
_VALUE_END_RE = re.compile(r"[,;}\]]")
# Inside quotes a value is a literal; only interpolation makes it code.
_INTERPOLATION_MARKS = ("${", "$(", "{{")
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


def _pattern_may_match(pattern: re.Pattern[str], text: str) -> bool:
    if not isinstance(pattern, re.Pattern):
        return True
    key = (getattr(pattern, "pattern", None), getattr(pattern, "flags", None))
    literals = _PATTERN_PREREQUISITES.get(key)
    if literals is None:
        return True
    return any(literal in text for literal in literals)


def _apply_pattern(pattern: re.Pattern[str], replacement: str, text: str) -> str:
    if not _pattern_may_match(pattern, text):
        return text
    return pattern.sub(replacement, text)


def _redact_patterns(text: str) -> str:
    out = text
    for pattern, replacement in _PATTERNS:
        out = _apply_pattern(pattern, replacement, out)
    return out


def _value_is_code(value: str, quoted: bool) -> bool:
    """`next(iterator)`, `tuple[bytes,`, `${{ secrets.X }}`, `?`, `128`.

    A quoted value is a literal, so `"Hunter$2024?"` is a password and not code;
    only interpolation inside the quotes names something stored elsewhere.
    """
    if value.isdigit():
        return True
    if quoted:
        return any(mark in value for mark in _INTERPOLATION_MARKS)
    return bool(_CODE_CHARACTERS & set(value))


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


# An unquoted all-letter value this long is a secret, not a word like `required`.
_MIN_ALPHA_SECRET_CHARS = 12
_QUOTED_VALUE_RE = re.compile(r"(\\?[\"'])(.*)(\1)", re.DOTALL)
_LOCATION_RE = re.compile(r"(?:/|~/|[A-Za-z]:\\|[a-z][a-z0-9+.-]*://)", re.IGNORECASE)


def _bare_value_is_credential(value: str) -> bool:
    if _matches_known_secret_shape(value):
        return True
    if value.isalpha():
        return len(value) >= _MIN_ALPHA_SECRET_CHARS
    return not _is_symbol_reference(value)


def _value_is_credential(value: str, quoted: bool) -> bool:
    """Whether the value after a credential-named key is a credential.

    The key names a slot; it does not prove the slot holds a secret. Declaring
    the type of the slot, assigning an expression to it, pointing at a secret
    stored elsewhere, or naming a path or a URL all leave the secret absent.
    """
    if len(value) < _MIN_CREDENTIAL_VALUE_CHARS or _value_is_code(value, quoted):
        return False
    if _LOCATION_RE.match(value):
        return False
    return True if quoted else _bare_value_is_credential(value)


def _split_value(raw: str) -> tuple[str, str, str, str]:
    """(opening quote, value, closing quote, what follows) — only a matching pair quotes."""
    quoted = _QUOTED_VALUE_RE.fullmatch(raw)
    if quoted is not None:
        return quoted.group(1), quoted.group(2), quoted.group(3), ""
    head = _VALUE_END_RE.split(raw, maxsplit=1)[0]
    return "", head, "", raw[len(head):]


def _replace_named_value(match: re.Match[str]) -> str:
    opening, value, closing, rest = _split_value(match.group(2))
    if not _value_is_credential(value, bool(opening)):
        return match.group(0)
    return f"{match.group(1)}{opening}[REDACTED]{closing}{rest}"


def _redact_named_values(text: str) -> str:
    out = text
    for pattern in _NAMED_VALUE_PATTERNS:
        out = _named_pattern_sub(pattern, out)
    return out


def _named_pattern_sub(pattern, text: str) -> str:
    if pattern is _CREDENTIAL_VALUE_PATTERN:
        return _indexed_named_values(text)
    return pattern.sub(_replace_named_value, text)


def _indexed_named_values(text: str) -> str:
    matches = (_CREDENTIAL_VALUE_PATTERN.match(text, head.start())
               for head in _ASSIGNMENT_HEAD.finditer(text))
    return _rewrite_named_matches(text, matches)


def _rewrite_named_matches(text: str, matches) -> str:
    pieces = []
    cursor = 0
    for match in matches:
        cursor = _append_named_match(text, match, cursor, pieces)
    pieces.append(text[cursor:])
    return "".join(pieces)


def _append_named_match(text: str, match, cursor: int, pieces) -> int:
    if match is None:
        return cursor
    if match.start() < cursor:
        return cursor
    pieces.extend((text[cursor:match.start()], _replace_named_value(match)))
    return match.end()


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


def describe_error(error: BaseException) -> str:
    """`Class: redacted message` — what a log needs to act on a failure.

    The class is the shape of the failure and the message the fact; a
    class alone (`RuntimeError`) let nobody act (audit OPS-09,
    docs/research/2026-09-10-a-failing-step-says-why-not-only-its-class.md).
    """
    message = redact_secrets(str(error)).strip()
    name = type(error).__name__
    return f"{name}: {message}" if message else name


# How many causes of a wrapped error a failure record names.
MAX_ERROR_CAUSES = 2


def _cause_of(error: BaseException) -> BaseException | None:
    if error.__cause__ is not None:
        return error.__cause__
    return error.__context__


def describe_error_chain(error: BaseException) -> str:
    """The error and up to two of its causes, `Class: message <- Cause: message`.

    A wrapper such as `ReliabilityV3ValidationError("reliability_v3_record_invalid")`
    raised `from` the real failure kept the only useful fact in `__cause__`, and the
    trail dropped it. See
    `docs/research/2026-09-14-a-worker-that-failed-lost-no-capture.md`.
    """
    parts = [describe_error(error)]
    cause = _cause_of(error)
    while cause is not None and len(parts) <= MAX_ERROR_CAUSES:
        parts.append(describe_error(cause))
        cause = _cause_of(cause)
    return " <- ".join(parts)


def redact_secrets(text: str) -> str:
    """Return text with common secret patterns replaced."""
    if not text or not isinstance(text, str):
        return text
    # Order is load-bearing: the key/value rules were the first six entries of
    # `_PATTERNS`, so `token=sk-…` collapsed to `token=[REDACTED]` and never to
    # `token=[REDACTED_API_KEY]`. Splitting them into their own pass must not
    # renumber that — the marker is asserted, hashed and stored downstream.
    return _redact_high_entropy(_redact_command_passwords(_CURL_COMMAND.sub(
        _curl_command, _redact_patterns(_redact_named_values(text))
    )))


# --- structured values ------------------------------------------------------
#
# A regex over serialized JSON cannot tell a value from the quote that ends it:
# `export PASSWORD=x` inside a JSON string became `…=[REDACTED],"description"…`,
# which is no longer JSON, and that turn dropped out of the session record
# (audit 2026-09-27 A-4). Structured data is redacted as structure: every string
# leaf through `redact_secrets`, every value under a secret-named key blanked.
# One walker for the queue, the blackboard, event payloads and transcripts.
# Research: docs/research/2026-09-27-a-secret-in-structure-is-redacted-as-structure.md
SECRET_KEYS = frozenset(
    {
        "api_key", "apikey", "authorization", "cookie", "credential", "credentials", "pass",
        "passwd", "passphrase", "password", "private_key", "secret", "set_cookie", "token",
    }
)
_SECRET_KEY_SUFFIXES = ("_api_key", "_authorization", "_cookie", "_credential", "_password", "_secret", "_token")


def is_secret_key(key: object) -> bool:
    """Whether this mapping key names a secret; a non-string key names none.

    JSON turns the other basic key types into "1", "true" and "null", so none of
    them can spell a secret. See
    `docs/research/2026-09-18-a-refusal-is-cheaper-than-a-crash.md`.
    """
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _redacted_mapping(value: dict) -> dict:
    return {key: "[REDACTED]" if is_secret_key(key) else redact_structure(item) for key, item in value.items()}


def _redacted_container(value: object) -> object:
    if isinstance(value, dict):
        return _redacted_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_structure(item) for item in value]
    return value


def redact_structure(value: object) -> object:
    """A copy of a JSON-shaped value with its secrets removed, structure intact."""
    if isinstance(value, str):
        return redact_secrets(value)
    return _redacted_container(value)


def _redacted_line(line: str) -> str:
    body = line.rstrip("\r\n")
    try:
        record = json.loads(body)
    except ValueError:
        return redact_secrets(line)
    ending = line[len(body):]
    return json.dumps(redact_structure(record), ensure_ascii=False, separators=(",", ":")) + ending


def redact_jsonl(text: str) -> str:
    """JSON Lines with each record redacted as structure; a line that is not JSON as text."""
    return "".join(_redacted_line(line) for line in text.splitlines(keepends=True))
