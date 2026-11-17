"""The value of one evidence reference: a daily entry and its exact byte span."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Bounded reads of evidence files; a file past its bound is refused by name, never
# cut, so a citation or an archive check fails rather than reading a partial file.
# A daily log read for archiving (also by archive_daily): the largest of 32 daily logs
# on the installed vault on 2026-09-27 was 867 318 bytes, so 16 MiB is about 19 times
# that; review if days grow toward it.
MAX_DAILY_BYTES = 16 * 1024 * 1024


_DAILY_ID_PATTERN = r"\d{4}-\d{2}-\d{2}"


SHA256_PATTERN = r"[0-9a-f]{64}"


_BLOCK_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}"


BLOCK_ID_RE = re.compile(_BLOCK_ID_PATTERN)


REF_RE = re.compile(
    rf"daily:(?P<daily>{_DAILY_ID_PATTERN}) "
    rf"sha256:(?P<sha>{SHA256_PATTERN}) "
    rf"block:(?P<block>{_BLOCK_ID_PATTERN}) "
    r"bytes:(?P<start>0|[1-9]\d*)-(?P<end>0|[1-9]\d*)"
)


class EvidenceResolutionError(ValueError):
    """Evidence could not be proven from one unambiguous immutable source."""


def _require_daily_id(daily_id: object) -> None:
    if not isinstance(daily_id, str) or re.fullmatch(_DAILY_ID_PATTERN, daily_id) is None:
        raise ValueError("evidence daily ID is invalid")
    try:
        if date.fromisoformat(daily_id).isoformat() != daily_id:
            raise ValueError
    except ValueError as exc:
        raise ValueError("evidence daily ID is invalid") from exc


def _require_source_digest(digest: object) -> None:
    if not isinstance(digest, str) or re.fullmatch(SHA256_PATTERN, digest) is None:
        raise ValueError("evidence SHA-256 is invalid")


def _require_block_identifier(block_id: object) -> None:
    if not isinstance(block_id, str) or BLOCK_ID_RE.fullmatch(block_id) is None:
        raise ValueError("evidence block ID is invalid")


def is_plain_int(value: object) -> bool:
    """A bool is an int in Python, and never a byte offset here."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_byte_span(start: object, end: object) -> None:
    if not is_plain_int(start) or not is_plain_int(end):
        raise ValueError("evidence byte span must be non-empty and half-open")
    if start < 0 or start >= end:
        raise ValueError("evidence byte span must be non-empty and half-open")


@dataclass(frozen=True)
class EvidenceRef:
    daily_id: str
    source_sha256: str
    block_id: str
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        _require_daily_id(self.daily_id)
        _require_source_digest(self.source_sha256)
        _require_block_identifier(self.block_id)
        _require_byte_span(self.byte_start, self.byte_end)

    @classmethod
    def parse(cls, value: str) -> EvidenceRef:
        if not isinstance(value, str):
            raise TypeError("evidence reference must be a string")
        match = REF_RE.fullmatch(value)
        if match is None:
            raise ValueError("evidence reference is not canonical")
        return cls(
            match["daily"],
            match["sha"],
            match["block"],
            int(match["start"]),
            int(match["end"]),
        )

    def __str__(self) -> str:
        return (
            f"daily:{self.daily_id} sha256:{self.source_sha256} "
            f"block:{self.block_id} bytes:{self.byte_start}-{self.byte_end}"
        )
