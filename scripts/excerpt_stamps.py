"""When and in which session an excerpt was written: what a reader needs beside its text.

A daily file holds every session of its day. Its entries are the ones
`evidence_resolver.daily_entries` defines — a `## [HH:MM:SS] event | session`
heading or an operation marker followed by a ``[HH:MM:SS] kind | session``
line — so an excerpt's date is its file's day and its entry's time, and its
session is the field of that line its writer puts it in (`_SESSION_FIELD`). A
compiled page is dated by its `valid_from`. Without either the reader saw one
file name for every session of a day and could not tell two sessions, or an
older value from a newer.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from evidence_resolver import EvidenceResolutionError, daily_entries

if TYPE_CHECKING:
    from corpus_snapshot import CapturedSource

_DAILY_FILE = re.compile(r"(?:^|/)daily/(\d{4}-\d{2}-\d{2})\.md$")
_MARKER = re.compile(r"^<!-- llm-wiki-operation:")
# Where each writer puts the session among the `|` fields: a heading and a
# prompt (`user_prompt_capture`) put it second, a tool line (`post_tool_capture`)
# names its agent first, and an episodes line (`episode_consolidation`) has none.
_SESSION_FIELD: dict[str, int | None] = {"tool": 2, "episodes": None}


@dataclass(frozen=True)
class ExcerptStamp:
    date: str = ""
    session: str = ""


@dataclass(frozen=True)
class EntryStamps:
    """The stamp of every entry of one source, by the bytes the entry spans."""

    starts: tuple[int, ...]
    ends: tuple[int, ...]
    stamps: tuple[ExcerptStamp, ...]
    page: ExcerptStamp

    @classmethod
    def of(cls, relative_path: str, content: bytes, valid_from: str | None = None) -> EntryStamps:
        day = _DAILY_FILE.search(relative_path.replace("\\", "/"))
        if day is None:
            return cls((), (), (), ExcerptStamp(valid_from or ""))
        entries = _entries(content)
        starts, ends = _spans(entries)
        stamps = tuple(_entry_stamp(day.group(1), content, entry) for entry in entries)
        return cls(starts, ends, stamps, ExcerptStamp(day.group(1)))

    def at(self, byte: int) -> ExcerptStamp:
        index = bisect.bisect_right(self.starts, byte) - 1
        if index < 0 or byte >= self.ends[index]:
            return self.page
        return self.stamps[index]


def _entries(content: bytes) -> list[tuple[str, int, int]]:
    """The file's entries; a file whose entries cannot be read is stamped by its day alone."""
    try:
        entries: list[tuple[str, int, int]] = daily_entries(content)
    except EvidenceResolutionError:
        return []
    return entries


def _spans(entries: list[tuple[str, int, int]]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    return tuple(entry[1] for entry in entries), tuple(entry[2] for entry in entries)


def _entry_stamp(day: str, content: bytes, entry: tuple[str, int, int]) -> ExcerptStamp:
    clock, start, end = entry
    return ExcerptStamp(f"{day} {clock}", _session(content[start:end]))


def _session(entry: bytes) -> str:
    """The session field of the line that declares the entry's time, by the entry's kind."""
    fields = [part.strip(" \t`") for part in _declaring_line(entry).split("|")]
    kind = next(reversed(fields[0].split()), "")
    position = _SESSION_FIELD.get(kind, 1)
    if position is None or position >= len(fields):
        return ""
    return fields[position]


def _declaring_line(entry: bytes) -> str:
    lines = entry.decode("utf-8", errors="replace").splitlines()
    return next((line for line in lines if line.strip() and not _MARKER.match(line)), "")


@dataclass
class StampBook:
    """Each source's entry stamps, read once per context."""

    by_path: dict[str, EntryStamps] = field(default_factory=dict)

    def of(self, source: CapturedSource, byte: int) -> ExcerptStamp:
        path = source.record.relative_path
        if path not in self.by_path:
            self.by_path[path] = EntryStamps.of(path, source.content, source.metadata.valid_from)
        return self.by_path[path].at(byte)
