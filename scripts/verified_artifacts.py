"""Digests already verified, remembered by stat identity.

Hashing every artifact of a generation against its manifest is what a cold open
pays: 241 MB of `evidence.sqlite3` and the rest, re-read in every new process to
learn what the previous process learned. A generation is immutable after
activation, so the answer cannot have changed unless the file did.

This is Git's index, narrowed to one job. Git stores each path's `lstat(2)` —
size, mtime, inode — and skips reading a file whose stat matches; an entry whose
mtime is *not strictly older* than the index's own mtime is "racily clean" and is
read anyway, because a file changed inside the same timestamp tick would
otherwise pass. Both halves are here, and the failure mode of everything else —
an unreadable file, a parse error, a missing entry — is a slow open, never a
trusted lie. Research:
`docs/research/2026-09-12-a-verdict-worth-remembering-across-processes.md`.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CACHE_RELATIVE_PATH = "cache/evidence-graph/verified-artifacts.json"
MAX_CACHE_BYTES = 1024 * 1024
MAX_ENTRIES = 512
SCHEMA_VERSION = "verified-artifacts/v1"


def cache_path(state_root: Path) -> Path:
    return Path(state_root).joinpath(*CACHE_RELATIVE_PATH.split("/"))


def _entry_key(generation_id: str, relative_path: str, metadata: os.stat_result) -> str:
    """One artifact's stat identity, as a single canonical string."""
    return "|".join(
        (
            str(generation_id),
            str(relative_path),
            str(metadata.st_dev),
            str(metadata.st_ino),
            str(metadata.st_size),
            str(metadata.st_mtime_ns),
        )
    )


def _loaded_document(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) > MAX_CACHE_BYTES:
        return {}
    document = json.loads(raw.decode("utf-8"))
    if document.get("schema_version") != SCHEMA_VERSION:
        return {}
    entries = document.get("entries")
    return entries if isinstance(entries, dict) else {}


def _cache_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


class VerifiedArtifacts:
    """What is remembered, and what this scan wants remembered next."""

    def __init__(self, state_root: Path) -> None:
        self.path = cache_path(state_root)
        self.entries = self._read()
        self.written_ns = _cache_mtime_ns(self.path)
        self.fresh: dict[str, str] = {}

    def _read(self) -> dict:
        try:
            return _loaded_document(self.path)
        except (OSError, UnicodeDecodeError, ValueError):
            # A cache that cannot be read is an empty cache.
            return {}

    def _is_racily_clean(self, metadata: os.stat_result) -> bool:
        """Git's rule: an artifact as new as the cache itself is not trusted."""
        return metadata.st_mtime_ns >= self.written_ns

    def remembered(
        self, generation_id: str, relative_path: str, metadata: os.stat_result
    ) -> str | None:
        """The digest verified for exactly these bytes, or None to hash them."""
        if self._is_racily_clean(metadata):
            return None
        stored = self.entries.get(_entry_key(generation_id, relative_path, metadata))
        return stored if isinstance(stored, str) else None

    def remember(
        self,
        generation_id: str,
        relative_path: str,
        metadata: os.stat_result,
        digest: str,
    ) -> None:
        self.fresh[_entry_key(generation_id, relative_path, metadata)] = digest

    def _merged(self) -> dict:
        merged = {**self.entries, **self.fresh}
        if len(merged) <= MAX_ENTRIES:
            return merged
        keep = list(merged)[-MAX_ENTRIES:]
        return {key: merged[key] for key in keep}

    def save(self) -> None:
        """Write what this scan verified; a failure to write costs a slow open."""
        if not self.fresh:
            return
        try:
            self._write(self._merged())
        except OSError:
            return

    def _write(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema_version": SCHEMA_VERSION, "entries": entries},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".verified-", suffix=".json"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise
