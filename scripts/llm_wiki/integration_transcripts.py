"""Reading a stable, bounded excerpt of an agent transcript."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from event_envelope import EventEnvelope
from llm_wiki import runtime_roots
from llm_wiki.integration_settings import MAX_CAPTURE_EVIDENCE_BYTES
from secret_redact import redact_jsonl


def _capture_path_is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _capture_path_text_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("capture transcript path is unavailable")
    if not value:
        raise ValueError("capture transcript path is unavailable")
    return value


def validated_capture_transcript_path(value: object) -> Path:
    text = _capture_path_text_value(value)
    path = Path(text).resolve(strict=True)
    if path.suffix.casefold() not in {".jsonl", ".json", ".txt", ".log"}:
        raise PermissionError("capture transcript extension is not allowed")
    from host_transcripts import host_transcript_roots

    roots = (*host_transcript_roots(), Path(runtime_roots.state_root()) / "cache" / "transient-transcripts")
    if not any(_capture_path_is_beneath(path, root) for root in roots):
        raise PermissionError("capture transcript path is not allowed")
    return path


def _read_transcript_edge(descriptor: int, offset: int, side: int) -> bytes:
    """One bounded side of an open transcript."""
    os.lseek(descriptor, max(offset, 0), os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = side
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_stable_transcript(before: os.stat_result, after: os.stat_result) -> None:
    """A file swapped mid-read must not become evidence."""
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("capture transcript changed while it was read")


# How far past its window an edge of a long transcript is searched for turns. A
# session's head can be all `file-history-snapshot` records; measured on this
# machine on 2026-09-26, two of six transcripts over the capture bound held no
# turn in their first 450 KiB, and filling the window with turns took up to 9.04
# windows of reading (0.23 s for 16). See
# `docs/research/2026-09-26-a-long-session-keeps-its-first-turns.md`.
EDGE_SCAN_WINDOWS = 16


def _transcript_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        getattr(info, "st_file_attributes", 0),
    )


def _require_transcript_identity(before: os.stat_result, after: os.stat_result) -> None:
    if _transcript_file_identity(before) != _transcript_file_identity(after):
        raise PermissionError("capture transcript identity changed while it was read")
    if after.st_size < before.st_size:
        raise ValueError("capture transcript shrank while it was read")


def _transcript_ranges(size: int, whole_limit: int | None, side: int) -> tuple[tuple[int, int], ...]:
    if whole_limit is not None and size <= whole_limit:
        return ((0, size),)
    scan = min(side * EDGE_SCAN_WINDOWS, size // 2)
    return ((0, scan), (size - scan, scan))


def _read_transcript_ranges(descriptor: int, ranges: tuple[tuple[int, int], ...]) -> tuple[bytes, ...]:
    pieces = tuple(_read_transcript_edge(descriptor, offset, length) for offset, length in ranges)
    if tuple(map(len, pieces)) != tuple(length for _, length in ranges):
        raise ValueError("capture transcript ended before its snapshot was read")
    return pieces


def _require_transcript_growth_or_stability(before: os.stat_result, after: os.stat_result) -> None:
    _require_transcript_identity(before, after)
    if after.st_size == before.st_size:
        _require_stable_transcript(before, after)


def _verify_transcript_ranges(
    descriptor: int, before: os.stat_result,
    ranges: tuple[tuple[int, int], ...], pieces: tuple[bytes, ...],
) -> None:
    after = os.fstat(descriptor)
    _require_transcript_growth_or_stability(before, after)
    if _read_transcript_ranges(descriptor, ranges) != pieces:
        raise ValueError("capture transcript content changed while it was read")
    verified = os.fstat(descriptor)
    _require_transcript_growth_or_stability(after, verified)


def _read_transcript_snapshot(
    path: Path, whole_limit: int | None, side: int,
) -> tuple[tuple[bytes, ...], int]:
    """Verify retained ranges of the initial file size; appended bytes wait for the next capture."""
    from bounded_io import require_regular_file, require_safe_ancestors

    require_safe_ancestors(path, "capture transcript", None)
    before = path.lstat()
    require_regular_file(path, before, "capture transcript")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _require_transcript_identity(before, opened)
        ranges = _transcript_ranges(opened.st_size, whole_limit, side)
        pieces = _read_transcript_ranges(descriptor, ranges)
        _verify_transcript_ranges(descriptor, opened, ranges, pieces)
        current = path.lstat()
        require_regular_file(path, current, "capture transcript")
        _require_transcript_identity(opened, current)
    finally:
        os.close(descriptor)
    return pieces, opened.st_size


def _turn_lines(lines: Iterable[bytes], side: int) -> list[bytes]:
    """Lines in the given order, service records skipped, until the window is full."""
    from session_evidence import is_service_record

    kept: list[bytes] = []
    used = 0
    for line in lines:
        if is_service_record(line.decode("utf-8", errors="replace")):
            continue
        used += len(line) + 1
        if used > side:
            break
        kept.append(line)
    return kept


def _turns_head(window: bytes, side: int) -> bytes:
    """The first turns; whole tokens of the raw window when not one whole turn fits in it."""
    kept = _turn_lines(window.split(b"\n")[:-1], side)
    return b"".join(line + b"\n" for line in kept) or whole_tokens_head(window[:side])


def _turns_tail(window: bytes, side: int) -> bytes:
    """The last turns; whole tokens of the raw window when not one whole turn fits in it."""
    lines = [line for line in window.split(b"\n")[1:] if line]
    kept = _turn_lines(reversed(lines), side)
    return b"".join(line + b"\n" for line in reversed(kept)) or whole_tokens_tail(window[-side:])


# Where a token of a JSON line ends. A raw window cut inside a token would keep a
# fragment the redactor cannot recognise — a token prefix is not a token — so the
# fragment at the cut is dropped (audit 2026-09-27 B-4,
# docs/research/2026-09-27-a-secret-is-redacted-before-it-is-cut.md).
_TOKEN_BOUNDARY = re.compile(rb"[\s,\"'{}\[\]:=]")


def whole_tokens_head(piece: bytes) -> bytes:
    ends = [match.end() for match in _TOKEN_BOUNDARY.finditer(piece)]
    return piece[: ends[-1]] if ends else b""


def whole_tokens_tail(piece: bytes) -> bytes:
    match = _TOKEN_BOUNDARY.search(piece)
    return piece[match.start():] if match else b""


def _capture_excerpt_marker(dropped: int) -> str:
    """One JSONL line of its own, so the record's renderer keeps it."""
    from session_evidence import capture_gap_line

    return f"\n{capture_gap_line(dropped)}\n"


def _render_capture_excerpt(head: bytes, tail: bytes, size: int) -> str:
    dropped = size - len(head) - len(tail)
    return _evidence_text(head) + _capture_excerpt_marker(dropped) + _evidence_text(tail)


def _evidence_text(data: bytes) -> str:
    """Evidence is kept around a byte that does not decode, and shows that it was there.

    A short transcript used to be decoded strictly and a long one with `ignore`: one
    stray byte lost the short session whole. See
    `docs/research/2026-09-17-four-small-capture-corrections.md`.
    """
    return data.decode("utf-8", errors="replace")


def capture_transcript_text(path: Path, limit: int = MAX_CAPTURE_EVIDENCE_BYTES) -> str:
    """The whole transcript, or a bounded excerpt of it, never nothing.

    A `pre_compact` hook fires because the conversation got long, so refusing
    every transcript over the evidence bound refused exactly the sessions worth
    keeping. Raising the bound is not available: measured on this machine on
    2026-08-26, 36 of 487 host transcripts are over it and the largest is 105 MB,
    and a hook cannot hold 105 MB to keep 900 KiB. Truncating loses nothing the
    bound was protecting, because the durable record this feeds is capped anyway
    -- `session_evidence.MAX_EVIDENCE_BYTES` keeps 512 KiB and appends its own
    truncation note. Head and tail rather than either alone: the same choice
    already recorded for nightly consolidation, because a long session puts its
    decisions early and its outcome late.

    `limit` is the bound this read keeps to; it is lowered when the text grew too
    much inside its JSON record (`_fitting_capture_record`).
    """
    from bounded_io import require_byte_limit

    require_byte_limit(limit)
    pieces, size = _read_transcript_snapshot(path, limit, limit // 2)
    if size <= limit:
        return _evidence_text(pieces[0])
    head, tail = pieces
    return _render_capture_excerpt(_turns_head(head, limit // 2), _turns_tail(tail, limit // 2), size)


def capture_path_evidence(
    value: object, limit: int = MAX_CAPTURE_EVIDENCE_BYTES
) -> str | None:
    """The transcript's text, or None when there is no transcript to read.

    `transcript_present` already handles a vanished transcript on the
    session-end branch. This is the other path to the same file, and it had no
    such guard: `resolve(strict=True)` raised `FileNotFoundError` and the whole
    capture was recorded as lost. Measured on this vault — five losses in four
    seconds on 2026-09-05, all sessions started outside any project, whose
    transcripts live under `-home-user`.

    Nothing is recovered by raising: if the file is gone, its contents are gone
    with it, and the session record itself is still worth keeping. Only the
    missing file is tolerated — a path outside the allowed roots or with a
    disallowed extension still raises `PermissionError`, because that is a
    refusal and not an absence.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        path = validated_capture_transcript_path(value)
    except FileNotFoundError:
        return None
    redacted = redact_jsonl(capture_transcript_text(path, limit))
    if not redacted:
        return None
    return redacted


def _capture_evidence_text(
    envelope: EventEnvelope,
    payload: Mapping[str, Any],
    limit: int = MAX_CAPTURE_EVIDENCE_BYTES,
) -> str | None:
    inline = envelope.payload.get("transcript_text")
    if isinstance(inline, str):
        if not inline:
            return None
        return inline
    return capture_path_evidence(payload.get("transcript_path"), limit)


def _capture_nullable_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} is invalid")
    return value


def capture_occurred_at(envelope: EventEnvelope) -> str | None:
    """When the session ended, as the envelope recorded it.

    The worker files the session record and the daily entry by this, so a queue
    drained the next morning does not file yesterday's sessions under today. See
    `docs/research/2026-09-17-a-session-is-filed-under-the-day-it-happened.md`.
    """
    occurred = getattr(envelope, "occurred_at", None)
    if not isinstance(occurred, datetime):
        return None
    return occurred.isoformat(timespec="microseconds")

