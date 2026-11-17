"""Extracted runtime filesystem owner; public compatibility remains in reliable_memory."""
from __future__ import annotations

from dataclasses import dataclass


class UnsafeStateRoot(ValueError):
    """Raised when runtime state cannot safely use local SQLite locking."""


class MetadataDurabilityUnavailable(OSError):
    """Raised when the platform cannot prove the requested metadata boundary."""

    code = "metadata_durability_unavailable"


@dataclass(frozen=True)
class RuntimeFileIdentity:
    platform: str
    volume: str
    file_id: str
    size: int
    mtime_ns: int


# Keep existing serialized public callable/class paths.
UnsafeStateRoot.__module__ = "reliable_memory"
MetadataDurabilityUnavailable.__module__ = "reliable_memory"
RuntimeFileIdentity.__module__ = "reliable_memory"
