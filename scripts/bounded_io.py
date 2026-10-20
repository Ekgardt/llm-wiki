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


class SourceChangedDuringRead(PermissionError):
    """The file was written or replaced while it was being read.

    Still a `PermissionError`, so every caller refuses as before. The type is
    for the caller that must tell "somebody is writing here, try again" from
    "this path is not safe to read". Research:
    `docs/research/2026-09-17-five-failures-only-the-other-systems-showed.md`.
    """


def _validated_deadline(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return _finite_deadline(deadline)


def _finite_deadline(deadline: object) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp or None")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


def _check_deadline(deadline: float | None, label: str) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(f"{label} read deadline expired")


def _root_controlled(info: os.stat_result) -> bool:
    """Only an administrator can write here."""
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _system_symlink(link: Path) -> bool:
    """A symlink an ordinary user could not have created or replaced.

    Traversal-resistant APIs draw the line here rather than at "no symlink
    anywhere": `openat2(RESOLVE_BENEATH)`, FreeBSD's `O_RESOLVE_BENEATH` and
    Go's `os.Root` all defend against constructs an unprivileged user can make
    and state plainly that ones requiring root are outside the threat model.
    macOS ships `/var` as a root-owned symlink to `/private/var`, so every
    temporary file on that platform sits behind one; refusing them all made
    this reader unusable there. A symlink qualifies only when both it and the
    directory holding it belong to root and are not group- or other-writable,
    which is exactly the "an administrator put it there" case. Symlink mode
    bits are meaningless on Linux, so the containing directory carries the
    decision.
    """
    if os.name == "nt":
        return False
    try:
        link_info = link.lstat()
        parent_info = link.parent.stat()
    except OSError:
        return False
    if link_info.st_uid != 0:
        return False
    return _root_controlled(parent_info)


def _acceptable_ancestor(parent: Path) -> bool:
    """Every ancestor is a real directory, or a system-owned symlink to one."""
    info = parent.lstat()
    if getattr(info, "st_file_attributes", 0) & 0x400:
        return False
    if parent.is_symlink():
        return _system_symlink(parent) and parent.is_dir()
    return stat.S_ISDIR(info.st_mode)


def _require_byte_limit(max_bytes: object) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _require_safe_ancestors(path: Path, label: str, deadline: float | None) -> None:
    for parent in path.parents:
        if parent == Path(parent.anchor):
            break
        _check_deadline(deadline, label)
        if not _acceptable_ancestor(parent):
            raise PermissionError(f"{label} parent must be a regular directory")


def _require_regular_file(path: Path, before: os.stat_result, label: str) -> None:
    if (
        path.is_symlink()
        or getattr(before, "st_file_attributes", 0) & 0x400
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PermissionError(f"{label} must be a regular non-symlink file")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _require_opened_identity(
    descriptor: int, identity: tuple[int, int, int, int], label: str
) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise PermissionError(f"{label} changed before open")
    if identity != _file_identity(opened):
        raise SourceChangedDuringRead(f"{label} changed before open")


def _read_bounded_chunks(
    descriptor: int, max_bytes: int, label: str, deadline: float | None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        _check_deadline(deadline, label)
        chunk = os.read(descriptor, min(IO_CHUNK_BYTES, max_bytes + 1 - total))
        _check_deadline(deadline, label)
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
    identity: tuple[int, int, int, int],
    label: str,
) -> None:
    after = os.fstat(descriptor)
    if identity != _file_identity(after):
        raise SourceChangedDuringRead(f"{label} changed during read")
    _require_same_file_at_path(path, identity, label)


def _require_same_file_at_path(
    path: Path, identity: tuple[int, int, int, int], label: str
) -> None:
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode):
        raise PermissionError(f"{label} was replaced during read")
    if identity[:2] != (current.st_dev, current.st_ino):
        raise SourceChangedDuringRead(f"{label} was replaced during read")


def _read_open_descriptor(
    descriptor: int,
    path: Path,
    identity: tuple[int, int, int, int],
    max_bytes: int,
    label: str,
    deadline: float | None,
) -> bytes:
    _check_deadline(deadline, label)
    _require_opened_identity(descriptor, identity, label)
    content = _read_bounded_chunks(descriptor, max_bytes, label, deadline)
    _check_deadline(deadline, label)
    _require_unchanged_after_read(descriptor, path, identity, label)
    _check_deadline(deadline, label)
    return content


def read_stable_bytes(
    path: Path,
    max_bytes: int,
    *,
    label: str = "file",
    deadline: float | None = None,
) -> bytes:
    deadline = _validated_deadline(deadline)
    path = Path(path)
    descriptor, before = _open_source_descriptor(path, max_bytes, label, deadline)
    try:
        return _read_open_descriptor(
            descriptor, path, _file_identity(before), max_bytes, label, deadline
        )
    finally:
        os.close(descriptor)


def _open_source_descriptor(path, max_bytes, label, deadline):
    _require_byte_limit(max_bytes)
    _require_safe_ancestors(path, label, deadline)
    _check_deadline(deadline, label)
    before = path.lstat()
    _require_regular_file(path, before, label)
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    _check_deadline(deadline, label)
    return os.open(path, flags), before


@contextmanager
def open_stable_range(path, max_bytes, *, label="file", deadline=None):
    """Compute one owned source proof, checking live identity through exit.

    The caller must finish consuming records before leaving this scope and must
    not publish an artifact until the scope exits successfully.
    """
    deadline = _validated_deadline(deadline)
    path = Path(path)
    descriptor, before = _open_source_descriptor(path, max_bytes, label, deadline)
    try:
        _require_opened_identity(descriptor, _file_identity(before), label)
        with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as stream:
            check = partial(_require_live_source, descriptor, path, before, label)
            source = VerifiedByteRange(stream, before.st_size, None, deadline, owner_check=check)
            yield source
            _require_final_source_content(source, label)
    finally:
        os.close(descriptor)


def _require_live_source(descriptor, path, before, label):
    opened = os.fstat(descriptor)
    expected = (*_file_identity(before), before.st_ctime_ns)
    if (*_file_identity(opened), opened.st_ctime_ns) != expected:
        raise SourceChangedDuringRead(f"{label} changed during read")
    _require_same_file_at_path(path, _file_identity(before), label)


def _require_final_source_content(source, label):
    source.require_active()
    source.stream.seek(0)
    try:
        _verified_original_blocks(source.stream, source.size, source.digest,
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
        digest, hashes = _verified_original_blocks(self.stream, self.size, self.digest,
                                                   self.deadline, self.chunk_size)
        object.__setattr__(self, 'digest', digest)
        object.__setattr__(self, 'hashes', hashes)
        self.stream.seek(0)
        self.require_active()

    def __len__(self):
        return self.size

    def require_active(self):
        _check_deadline(self.deadline, 'original source')
        if self.stream.closed:
            raise ValueError('original range scope is closed')
        if self.owner_check is not None:
            self.owner_check()

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
        block = _read_original_block(self.stream, size, self.deadline)
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


def _verified_original_blocks(stream, size, expected, deadline, chunk_size):
    _require_range_identity(expected, chunk_size)
    if type(size) is not int or size < 0:
        raise ValueError('invalid original source size')
    digest, blocks = _original_block_proof(stream, size, deadline, chunk_size)
    _require_expected_source_digest(expected, digest)
    return digest, blocks


def _original_block_proof(stream, size, deadline, chunk_size):
    digest, blocks = hashlib.sha256(), []
    for start in range(0, size, chunk_size):
        chunk = _read_original_block(stream, min(chunk_size, size - start), deadline)
        digest.update(chunk)
        blocks.append(hashlib.sha256(chunk).digest())
    _require_original_eof(stream, deadline)
    return digest.hexdigest(), tuple(blocks)


def _require_expected_source_digest(expected, actual):
    if expected is not None and actual != expected:
        raise ValueError('snapshot content digest mismatch')


def _read_original_block(stream, size, deadline):
    chunks = []
    while size:
        _check_deadline(deadline, 'original source')
        chunk = stream.read(size)
        _check_deadline(deadline, 'original source')
        if not chunk:
            raise ValueError('original source truncated during verification')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def _require_original_eof(stream, deadline):
    _check_deadline(deadline, 'original source')
    extra = stream.read(1)
    _check_deadline(deadline, 'original source')
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
