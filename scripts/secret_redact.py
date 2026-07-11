"""Shared secret redaction for durable daily-log capture.

Pattern set informed by gitleaks v8.30.1 (per-rule Shannon entropy) and
TruffleHog v3.95.9 (800+ detectors with active verification). This module
is a best-effort real-time redactor for hook-level ms-latency usage — it
is NOT a full DLP scanner. For CI secret scanning, rely on gitleaks.
"""
from __future__ import annotations

import math
import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(secret\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(token\s*[=:]\s*)(\S+)"), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PEM_KEY]"),
]

_HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"
)
_PURE_HEX_RE = re.compile(r"^[0-9a-f]+$")
_ENTROPY_THRESHOLD = 4.0


def _shannon_entropy(data: str) -> float:
    if not data:
        return 0.0
    freq: dict[str, int] = {}
    for c in data:
        freq[c] = freq.get(c, 0) + 1
    n = len(data)
    return -sum((f / n) * math.log2(f / n) for f in freq.values())


def redact_secrets(text: str) -> str:
    """Return text with common secret patterns replaced."""
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    for m in _HIGH_ENTROPY_RE.finditer(out):
        token = m.group()
        if _PURE_HEX_RE.match(token):
            continue
        if _shannon_entropy(token) >= _ENTROPY_THRESHOLD:
            out = out.replace(token, "[REDACTED_TOKEN]")
    return out
