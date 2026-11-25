"""When and in which session an excerpt was written: what a reader needs beside its text.

A daily file holds every session of its day, each under a heading
`## [HH:MM:SS] event | session`, so an excerpt's date is its file's day and its
entry's time, and its session is the heading's last field. A compiled page is
dated by its `valid_from`. Without either the reader saw one file name for every
session of a day and could not tell two sessions, or an older value from a newer.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corpus_snapshot import CapturedSource

_DAILY_FILE = re.compile(r"(?:^|/)daily/(\d{4}-\d{2}-\d{2})\.md$")
_ENTRY_HEADING = re.compile(
    rb"^## \[(\d{2}:\d{2}(?::\d{2})?)\][^|\n]*(?:\|[ \t]*([^\n]*\S))?", re.MULTILINE
)


@dataclass(frozen=True)
class ExcerptStamp:
    date: str = ""
    session: str = ""


@dataclass(frozen=True)
class EntryStamps:
    """The stamp of every entry of one source, by the byte its heading starts at."""

    starts: tuple[int, ...]
    stamps: tuple[ExcerptStamp, ...]
    page: ExcerptStamp

    @classmethod
    def of(cls, relative_path: str, content: bytes, valid_from: str | None = None) -> EntryStamps:
        day = _DAILY_FILE.search(relative_path.replace("\\", "/"))
        if day is None:
            return cls((), (), ExcerptStamp(valid_from or ""))
        headings = list(_ENTRY_HEADING.finditer(content))
        stamps = tuple(_entry_stamp(day.group(1), heading) for heading in headings)
        return cls(
            tuple(heading.start() for heading in headings), stamps, ExcerptStamp(day.group(1))
        )

    def at(self, byte: int) -> ExcerptStamp:
        index = bisect.bisect_right(self.starts, byte) - 1
        if index < 0:
            return self.page
        return self.stamps[index]


def _entry_stamp(day: str, heading: re.Match[bytes]) -> ExcerptStamp:
    clock = heading.group(1).decode("ascii")
    session = (heading.group(2) or b"").decode("utf-8", errors="replace")
    return ExcerptStamp(f"{day} {clock}", session)


@dataclass
class StampBook:
    """Each source's entry stamps, read once per context."""

    by_path: dict[str, EntryStamps] = field(default_factory=dict)

    def of(self, source: CapturedSource, byte: int) -> ExcerptStamp:
        path = source.record.relative_path
        if path not in self.by_path:
            self.by_path[path] = EntryStamps.of(path, source.content, source.metadata.valid_from)
        return self.by_path[path].at(byte)
