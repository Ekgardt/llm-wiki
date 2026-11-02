"""Source descriptors and their canonical identity contract."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class SourceOccurrenceBounds:
    first_event_id: str
    last_event_id: str

    def canonical(self) -> dict[str, str]:
        for value in (self.first_event_id, self.last_event_id):
            if not isinstance(value, str) or not 1 <= len(value.encode()) <= 256:
                raise ValueError("source event ID is invalid")
        return {
            "first_event_id": self.first_event_id,
            "last_event_id": self.last_event_id,
        }


@dataclass(frozen=True, order=True)
class SourceDescriptor:
    logical_path: str
    byte_length: int
    sha256: str
    occurrence_bounds: SourceOccurrenceBounds | None = None

    def canonical(self) -> list[object]:
        _validate_logical_path(self.logical_path)
        if not isinstance(self.byte_length, int) or isinstance(self.byte_length, bool):
            raise TypeError("source byte length must be an integer")
        if self.byte_length < 0:
            raise ValueError("source byte length must be non-negative")
        _validate_digest(self.sha256, "source SHA-256")
        return [self.logical_path, self.byte_length, self.sha256]

    def receipt_descriptor(self) -> dict[str, object]:
        self.canonical()
        return {
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "byte_size": self.byte_length,
            "occurrence_bounds": (
                None
                if self.occurrence_bounds is None
                else self.occurrence_bounds.canonical()
            ),
        }


def _validate_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_logical_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("source logical path must be relative POSIX syntax")
    if _escapes_vault(value):
        raise ValueError("source logical path must remain inside the vault")


def _escapes_vault(value: str) -> bool:
    path = PurePosixPath(value)
    if not path.parts or value == "." or path.is_absolute():
        return True
    return ".." in path.parts or re.match(r"^[A-Za-z]:", value) is not None
