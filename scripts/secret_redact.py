"""Shared secret redaction for durable daily-log capture."""
from __future__ import annotations

import re

# Common credential patterns (best-effort; not a full DLP scanner).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    # High-entropy-ish long base64-ish tokens (min length to reduce false positives)
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_TOKEN]"),
]


def redact_secrets(text: str) -> str:
    """Return text with common secret patterns replaced."""
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out
