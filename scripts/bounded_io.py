"""Bounded, no-follow, stable reads for compile-time authoritative files."""
from __future__ import annotations

import codecs
import hashlib
import math
import operator
import os
import re
import stat
import time
from array import array
from bisect import bisect_left
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from llm_wiki.path_ancestry import acceptable_ancestor as acceptable_ancestor
from llm_wiki.path_ancestry import root_controlled as root_controlled
from llm_wiki.path_ancestry import system_symlink as system_symlink

# The ceiling for one Markdown page under `knowledge/`, declared once: every
# reader of a page (journal, claim tree, guardrails snapshot, corpus, search,
# compile after-image, index rebuild, backlink repair, access telemetry)
# aliases this, so a page one of them accepts is never one another refuses.
# Measured 2026-09-09: a 4.2 MB journal over a 4 MiB reader cap stopped every
# compile for three days. Research:
# docs/research/2026-09-10-one-page-ceiling-for-every-reader-of-knowledge.md
MAX_KNOWLEDGE_PAGE_BYTES = 8 * 1024 * 1024
# One read or hash step over any bounded file. It was defined six times under
# five names before 2026-09-23; see
# `docs/research/2026-09-23-one-limit-one-place.md`.
IO_CHUNK_BYTES = 64 * 1024
# How old a file's change stamp must be before an unchanged stat proves the
# file unchanged. Operating systems stamp files from a coarse clock: Linux
# advances it once per jiffy (10 ms at the lowest supported CONFIG_HZ of 100),
# Windows once per system timer tick (15.6 ms by default). A second write in
# the same tick carries the same stamp, so an observation is trusted only once
# the stamp is older than a tick (git's racily-clean rule). Research:
# `docs/research/2026-11-06-a-rewrite-in-the-same-tick-is-a-change.md`.
TIMESTAMP_SETTLE_NS = 20_000_000
# How long a reader keeps looking at a file that is still being written before
# it refuses it: fifty settle windows, enough for a burst of writes to stop, and
# well inside the 60-second default timeout of an agent hook. A caller's earlier
# deadline ends the wait sooner; a later one leaves the rest of its time to the
# caller's other work.
SETTLE_BUDGET_NS = 1_000_000_000


class SourceChangedDuringRead(PermissionError):
    """The file was written or replaced while it was being read.

    Still a `PermissionError`, so every caller refuses as before. The type is
    for the caller that must tell "somebody is writing here, try again" from
    "this path is not safe to read". Research:
    `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
    """


def validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return _finite_deadline(deadline)


def _finite_deadline(deadline: object) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp or None")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def check_deadline(deadline: float | None, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f"{label} read deadline expired")


def require_byte_limit(max_bytes: object) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def require_safe_ancestors(path: Path, label: str, deadline: float | None) -> None:
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        check_deadline(deadline, label)
        if not acceptable_ancestor(parent):
            raise PermissionError(f"{label} parent must be a regular directory")


def require_regular_file(path: Path, before: os.stat_result, label: str) -> None:
    if (
        path.is_symlink()
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink file")


FileIdentity = tuple[int, int, int, int, int]


def file_identity(info: os.stat_result) -> FileIdentity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, _change_time_ns(info))


def change_stamp_ns(info: os.stat_result) -> int:
    """The stamp every write moves: ctime on POSIX, which `utime` cannot set.

    Windows reports the creation time as ctime, so mtime is its write stamp.
    """
    if os.name == "nt":
        return info.st_mtime_ns
    return info.st_ctime_ns


def _change_time_ns(info: os.stat_result) -> int:
    if os.name == "nt":
        return 0
    return info.st_ctime_ns


def settled_lstat(
    path: Path, *, label: str = "file", deadline: float | None = None
) -> os.stat_result:
    """An lstat whose change stamp no write from now on can carry.

    A stamp younger than one clock tick is looked at again until its tick has
    passed; a write meanwhile gives a newer stamp, which is waited out the same
    way. `SETTLE_BUDGET_NS`, or an earlier caller deadline, ends a file that never
    stops being written. Callers that check the path's ancestors do so after this
    returns, so the wait cannot separate that check from the stat. Research:
    `docs/research/2026-11-06-a-rewrite-in-the-same-tick-is-a-change.md`.
    """
    until, give_up = _settle_limit(deadline, label)
    observed = path.lstat()
    wait_ns = _settle_wait_ns(observed)
    while wait_ns:
        _sleep_until_settled(wait_ns, until, give_up)
        observed = path.lstat()
        wait_ns = _settle_wait_ns(observed)
    return observed


def _settle_limit(deadline: float | None, label: str) -> tuple[float, OSError]:
    """The earlier of the settle budget and the caller's deadline, and its refusal."""
    budget_ends = time.monotonic() + SETTLE_BUDGET_NS / 1_000_000_000
    if deadline is None or budget_ends <= deadline:
        return budget_ends, SourceChangedDuringRead(f"{label} is still being written")
    return deadline, TimeoutError(f"{label} read deadline expired")


def _settle_wait_ns(info: os.stat_result) -> int:
    """How long until no write can share the stamp; none for one over a tick ahead.

    A write is stamped with the current tick, so it cannot carry a stamp still
    more than a tick in the future (a clock stepped back, a synced mtime).
    """
    ahead_ns = change_stamp_ns(info) - time.time_ns()
    if ahead_ns > TIMESTAMP_SETTLE_NS:
        return 0
    return max(ahead_ns + TIMESTAMP_SETTLE_NS, 0)


def _sleep_until_settled(wait_ns: int, until: float, give_up: OSError) -> None:
    wait_seconds = wait_ns / 1_000_000_000
    if time.monotonic() + wait_seconds >= until:
        raise give_up
    time.sleep(wait_seconds)


def _require_opened_identity(
    descriptor: int, identity: FileIdentity, label: str
) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError(f"{label} changed before open")
    if identity != file_identity(opened):
        raise SourceChangedDuringRead(f"{label} changed before open")
    return opened


def path_descriptor_change_time_matches(before, opened):
    """Compare like timestamps across Windows path/descriptor stat APIs."""
    if os.name == "nt":
        return (getattr(before, "st_birthtime_ns", before.st_ctime_ns)
                == getattr(opened, "st_birthtime_ns", opened.st_ctime_ns))
    return before.st_ctime_ns == opened.st_ctime_ns


def _read_bounded_chunks(
    descriptor: int, max_bytes: int, label: str, deadline: float | None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        check_deadline(deadline, label)
        chunk = os.read(descriptor, min(IO_CHUNK_BYTES, max_bytes + 1 - total))
        check_deadline(deadline, label)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    return content


def _require_unchanged_after_read(
    descriptor: int,
    path: Path,
    identity: FileIdentity,
    label: str,
) -> None:
    after = os.fstat(descriptor)
    if identity != file_identity(after):
        raise SourceChangedDuringRead(f"{label} changed during read")
    require_same_file_at_path(path, identity, label)


def require_same_file_at_path(
    path: Path, identity: FileIdentity, label: str
) -> None:
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode):
        raise PermissionError(f"{label} was replaced during read")
    if identity[:2] != (current.st_dev, current.st_ino):
        raise SourceChangedDuringRead(f"{label} was replaced during read")


def read_open_descriptor(
    descriptor: int,
    path: Path,
    identity: FileIdentity,
    max_bytes: int,
    label: str,
    deadline: float | None,
) -> bytes:
    check_deadline(deadline, label)
    _require_opened_identity(descriptor, identity, label)
    content = _read_bounded_chunks(descriptor, max_bytes, label, deadline)
    check_deadline(deadline, label)
    _require_unchanged_after_read(descriptor, path, identity, label)
    check_deadline(deadline, label)
    return content


def read_stable_bytes(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> bytes:
    deadline = validated_deadline(deadline)
    path = Path(path)
    descriptor, before = _open_source_descriptor(path, max_bytes, label, deadline)
    try:
        return read_open_descriptor(
            descriptor, path, file_identity(before), max_bytes, label, deadline
        )
    finally:
        os.close(descriptor)


def _open_source_descriptor(path, max_bytes, label, deadline):
    require_byte_limit(max_bytes)
    check_deadline(deadline, label)
    before = settled_lstat(path, label=label, deadline=deadline)
    require_safe_ancestors(path, label, deadline)
    require_regular_file(path, before, label)
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    check_deadline(deadline, label)
    return os.open(path, flags), before


@contextmanager
def open_stable_range(path, max_bytes, *, label="file", deadline=None):
    """Compute one owned source proof, checking live identity through exit.

    The caller must finish consuming records before leaving this scope and must
    not publish an artifact until the scope exits successfully.
    """
    deadline = validated_deadline(deadline)
    path = Path(path)
    descriptor, before = _open_source_descriptor(path, max_bytes, label, deadline)
    try:
        opened = _require_opened_identity(descriptor, file_identity(before), label)
        if not path_descriptor_change_time_matches(before, opened):
            raise SourceChangedDuringRead(f"{label} changed before open")
        with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as stream:
            check = partial(_require_live_source, descriptor, path, opened, label)
            source = VerifiedByteRange(stream, before.st_size, None, deadline, owner_check=check)
            yield source
            source.verify_content(label=label)
    finally:
        os.close(descriptor)


def _require_live_source(descriptor, path, before, label):
    opened = os.fstat(descriptor)
    expected = (*file_identity(before), before.st_ctime_ns)
    if (*file_identity(opened), opened.st_ctime_ns) != expected:
        raise SourceChangedDuringRead(f"{label} changed during read")
    require_same_file_at_path(path, file_identity(before), label)


def _require_final_source_content(source, label):
    source.require_active()
    source.stream.seek(0)
    try:
        verified_original_blocks(source.stream, source.size, source.digest,
                                  source.deadline, source.chunk_size)
    except ValueError as exc:
        raise SourceChangedDuringRead(f"{label} content changed during read") from exc
    source.require_active()




@dataclass
class _VerifiedRangeCursor:
    cached: tuple = (-1, b'')
    next_index: int = 0
    newlines: memoryview | None = None
    utf8_valid: bool = False


@dataclass(frozen=True, eq=False)
class VerifiedByteRange:
    """Owned verified source; immutable identity, bounded mutable read cursor.

    The stream owner must keep the stream open. Each reloaded block is checked
    against its initial hash; cached blocks are immutable captured bytes.
    """

    stream: object = field(repr=False)
    size: int
    digest: str | None
    deadline: float | None = None
    chunk_size: int = IO_CHUNK_BYTES
    owner_check: Callable | None = field(default=None, repr=False)
    hashes: tuple = field(init=False, repr=False)
    _cursor: _VerifiedRangeCursor = field(default_factory=_VerifiedRangeCursor, init=False, repr=False)

    def __post_init__(self):
        digest, hashes = verified_original_blocks(self.stream, self.size, self.digest,
                                                   self.deadline, self.chunk_size)
        object.__setattr__(self, 'digest', digest)
        object.__setattr__(self, 'hashes', hashes)
        self.stream.seek(0)
        self.require_active()

    def __len__(self):
        return self.size

    def require_active(self):
        check_deadline(self.deadline, 'original source')
        if self.stream.closed:
            raise ValueError('original range scope is closed')
        if self.owner_check is not None:
            self.owner_check()

    def verify_content(self, *, label="original source"):
        """Re-read the captured size and verify identity before releasing ownership."""
        self.require_active()
        position = self.stream.tell()
        try:
            _require_final_source_content(self, label)
        finally:
            self.stream.seek(position)

    def iter_chunks(self, start, end):
        self.require_active()
        require_byte_range(start, end, self.size)
        while start < end:
            index, offset = divmod(start, self.chunk_size)
            block = self._block(index)
            take = min(len(block) - offset, end - start)
            yield block[offset:offset + take]
            start += take

    def _block(self, index):
        self.require_active()
        if self._cursor.cached[0] == index:
            return self._cursor.cached[1]
        return self._load_block(index)

    def _load_block(self, index):
        if index < self._cursor.next_index:
            self.stream.seek(0)
            self._cursor.next_index = 0
        while self._cursor.next_index <= index:
            block = self._read_proven_block(self._cursor.next_index)
            self._cursor.cached = self._cursor.next_index, block
            self._cursor.next_index += 1
        return self._cursor.cached[1]

    def _read_proven_block(self, index):
        size = min(self.chunk_size, self.size - index * self.chunk_size)
        block = read_original_block(self.stream, size, self.deadline)
        if hashlib.sha256(block).digest() != self.hashes[index]:
            raise ValueError('original source range changed after verification')
        return block

    def range_digest(self, start, end):
        self.require_active()
        require_byte_range(start, end, self.size)
        if start == 0 and end == self.size:
            return self.digest
        digest = hashlib.sha256()
        for chunk in self.iter_chunks(start, end):
            digest.update(chunk)
        return digest.hexdigest()

    def __getitem__(self, key):
        self.require_active()
        if isinstance(key, slice):
            return self._slice(key)
        index = operator.index(key)
        index = index + self.size if index < 0 else index
        if not 0 <= index < self.size:
            raise IndexError('original range index out of bounds')
        return self._block(index // self.chunk_size)[index % self.chunk_size]

    def _slice(self, key):
        start, end, step = key.indices(self.size)
        if not range(start, end, step):
            return b''
        if step < 0:
            start, end = end + 1, start + 1
        return b''.join(self.iter_chunks(start, end))[::step]

    def newline_offsets(self, *, validate_utf8=False):
        self.require_active()
        if self._cursor.newlines is None:
            self._cursor.newlines = _immutable_offsets(_original_newlines(
                self.iter_chunks(0, self.size), validate_utf8))
            self._cursor.utf8_valid = validate_utf8
        if validate_utf8 and not self._cursor.utf8_valid:
            _validate_original_utf8(self.iter_chunks(0, self.size))
            self._cursor.utf8_valid = True
        return self._cursor.newlines

    def find(self, needle, start=0, end=None):
        self.require_active()
        needle = memoryview(needle).tobytes()
        bounds = _original_search_bounds(self.size, start, end)
        return _search_original(self, needle, bounds, reverse=False)

    def rfind(self, needle, start=0, end=None):
        self.require_active()
        needle = memoryview(needle).tobytes()
        bounds = _original_search_bounds(self.size, start, end)
        return _search_original(self, needle, bounds, reverse=True)


def _immutable_offsets(values):
    return memoryview(array('Q', values).tobytes()).cast('Q')


def _original_newlines(chunks, validate_utf8):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='strict') if validate_utf8 else None
    return _decoded_newline_positions(chunks, decoder)


def _decoded_newline_positions(chunks, decoder):
    offset = 0
    for chunk in chunks:
        if decoder is not None:
            decoder.decode(chunk, final=False)
        yield from (offset + match.start() for match in re.finditer(b"\n", chunk))
        offset += len(chunk)
    if decoder is not None:
        decoder.decode(b'', final=True)


def _validate_original_utf8(chunks):
    decoder = codecs.getincrementaldecoder('utf-8')(errors='strict')
    for chunk in chunks:
        decoder.decode(chunk, final=False)
    decoder.decode(b'', final=True)


def _search_original(content, needle, bounds, *, reverse):
    if bounds is None:
        return -1
    if needle == b'\n':
        return _search_newline(content.newline_offsets(), bounds, reverse)
    return _search_original_bytes(content, needle, bounds, reverse)


def _search_newline(offsets, bounds, reverse):
    first, last = (bisect_left(offsets, bound) for bound in bounds)
    if first == last:
        return -1
    return offsets[last - 1] if reverse else offsets[first]


def _search_original_bytes(content, needle, bounds, reverse):
    if len(needle) > bounds[1] - bounds[0]:
        return -1
    if not needle:
        return bounds[1] if reverse else bounds[0]
    search = _last_original_match if reverse else _find_original_chunks
    return search(content.iter_chunks(*bounds), needle, bounds[0])


def verified_original_blocks(stream, size, expected, deadline, chunk_size):
    _require_range_identity(expected, chunk_size)
    if type(size) is not int or size < 0:
        raise ValueError('invalid original source size')
    digest, blocks = _original_block_proof(stream, size, deadline, chunk_size)
    _require_expected_source_digest(expected, digest)
    return digest, blocks


def _original_block_proof(stream, size, deadline, chunk_size):
    digest, blocks = hashlib.sha256(), []
    for start in range(0, size, chunk_size):
        chunk = read_original_block(stream, min(chunk_size, size - start), deadline)
        digest.update(chunk)
        blocks.append(hashlib.sha256(chunk).digest())
    _require_original_eof(stream, deadline)
    return digest.hexdigest(), tuple(blocks)


def _require_expected_source_digest(expected, actual):
    if expected is not None and actual != expected:
        raise ValueError('snapshot content digest mismatch')


def read_original_block(stream, size, deadline):
    chunks = []
    while size:
        check_deadline(deadline, 'original source')
        chunk = stream.read(size)
        check_deadline(deadline, 'original source')
        if not chunk:
            raise ValueError('original source truncated during verification')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def _require_original_eof(stream, deadline):
    check_deadline(deadline, 'original source')
    extra = stream.read(1)
    check_deadline(deadline, 'original source')
    if extra:
        raise ValueError('original source extent changed during verification')


def _original_search_bounds(size, start, end):
    start = 0 if start is None else operator.index(start)
    first, last, _ = slice(start, end).indices(size)
    if start > size:
        return None
    if first > last:
        return None
    return first, last


def _original_search_windows(chunks, needle, start):
    tail, cursor = b'', start
    for chunk in chunks:
        window = tail + chunk
        yield cursor - len(tail), window
        tail = window[len(window) - min(len(window), len(needle) - 1):]
        cursor += len(chunk)


def _find_original_chunks(chunks, needle, start):
    if not needle:
        return start
    for offset, window in _original_search_windows(chunks, needle, start):
        found = window.find(needle)
        if found >= 0:
            return offset + found
    return -1


def _last_original_match(chunks, needle, start):
    result = -1
    for offset, window in _original_search_windows(chunks, needle, start):
        found = window.rfind(needle)
        if found >= 0:
            result = offset + found
    return result


def _require_range_identity(digest, chunk_size):
    if digest is not None:
        _require_source_digest(digest)
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError('source transfer size must be a positive integer')


def _require_source_digest(digest):
    if not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
        raise ValueError('source identity must be a SHA-256 hex digest')


def require_byte_range(start, end, size):
    if not _valid_original_bounds(start, end, size):
        raise ValueError('invalid original source range')


def _valid_original_bounds(start, end, size):
    return type(start) is int and type(end) is int and 0 <= start <= end <= size




def require_immutable_source(content):
    if type(content) is bytes:
        return
    if type(content) is not VerifiedByteRange:
        raise ValueError('source requires immutable bytes or an owned verified range')
    content.require_active()


def immutable_source_digest(content):
    require_immutable_source(content)
    if type(content) is VerifiedByteRange:
        return content.digest
    return hashlib.sha256(content).hexdigest()


def same_immutable_source(left, right):
    require_immutable_source(left)
    require_immutable_source(right)
    if left is right:
        return True
    if type(left) is bytes and type(right) is bytes:
        return left == right
    return len(left) == len(right) and immutable_source_digest(left) == immutable_source_digest(right)
