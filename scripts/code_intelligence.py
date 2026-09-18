"""The normalized code-intelligence values the navigation path reads.

What was here besides these — the analyzer run, claim, coverage, validity and
verified-batch records of the superseded 2026-07-21 Plan A, and the
`verify_native_analysis` / `closed_world` verifiers — was removed on 2026-09-18
with the `evidence-graph/v3` schema it filled: nothing in production built one.
See `docs/research/2026-09-18-the-superseded-plan-a-seam-leaves-the-code.md`.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum

_SQLITE_INT64_MAX = 2**63 - 1


class Capability(str, Enum):
    DEFINITIONS = "definitions"
    DECLARATIONS = "declarations"
    REFERENCES = "references"
    CALLS = "calls"
    IMPORTS = "imports"
    TYPES = "types"
    TYPE_DEFINITIONS = "type_definitions"
    INHERITANCE = "inheritance"
    IMPLEMENTATIONS = "implementations"
    DIAGNOSTICS = "diagnostics"


class PositionEncoding(str, Enum):
    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


def _require_text(value: object, label: str, *, maximum: int, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    _require_bounded_utf8(value, label, maximum)
    return value


def _require_bounded_utf8(value: str, label: str, maximum: int) -> None:
    encoded = _utf8(value, label)
    if not encoded:
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    _require_nfc(value, label)


def _utf8(value: str, label: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc


def _require_nfc(value: str, label: str) -> None:
    if not unicodedata.is_normalized("NFC", value):
        raise ValueError(f"{label} must use NFC normalization")


def _require_sqlite_int(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    _require_int64_range(value, label, minimum)
    return value


def _require_int64_range(value: int, label: str, minimum: int) -> None:
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if value > _SQLITE_INT64_MAX:
        raise ValueError(f"{label} must fit a signed int64")


@dataclass(frozen=True, slots=True)
class PositionRange:
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        for name in ("byte_start", "byte_end"):
            _require_sqlite_int(getattr(self, name), name, minimum=0)
        if self.byte_end < self.byte_start:
            raise ValueError("byte_end must not precede byte_start")

    def require_nonempty(self, label: str) -> PositionRange:
        _require_text(label, "range label", maximum=128)
        if self.byte_end <= self.byte_start:
            raise ValueError(f"{label} must use a non-empty half-open byte range")
        return self


