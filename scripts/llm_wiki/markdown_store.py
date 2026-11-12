"""Markdown transaction helpers shared by the coordinator parts and the public writers."""

from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import process_liveness
from bounded_io import IO_CHUNK_BYTES, VerifiedByteRange
from iso_time import utc_text
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_records import (
    RefusedOperation,
    TargetBoundaryFailure,
    TargetPathBoundaryError,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_deadlines import (
    busy_database,
)
from llm_wiki.windows_acl import (
    harden_windows_acl,
)
from llm_wiki.windows_native import (
    windows_kernel32,
)
from reliable_memory import (
    set_owner_only,
)

Validator = Callable[[Mapping[str, object]], object]
ABSENT = "absent"


# Plans are written as v2, which adds the append encoding (an append keeps only the
# bytes it added and the before length); v1 plans, full images only, stay readable.
# docs/research/2026-09-28-an-append-keeps-only-what-it-added.md
SCHEMA_DIRECTORY = Path(__file__).resolve().parents[1] / "schemas"
PLAN_SCHEMA = SCHEMA_DIRECTORY / "markdown-transaction-v2.json"


_PLAN_SCHEMAS = {
    "markdown-transaction/v1": SCHEMA_DIRECTORY / "markdown-transaction-v1.json",
    "markdown-transaction/v2": PLAN_SCHEMA,
}


# The writer gate's lease; it must outlive many heartbeats (below) so one late beat never loses the gate.
WRITER_LEASE_SECONDS = 30.0


# The writer's heartbeat: 60 beats per lease. Coordination interval, not operator-tunable.
WRITER_HEARTBEAT_SECONDS = 0.5
WRITER_WAIT_SECONDS = DEFAULTS.markdown_busy_ms / 1_000


# First retry delay for a busy writer gate, doubled per attempt up to the cap below.
_WRITER_RETRY_BASE_SECONDS = 0.005


# Retry delay cap: short against the hook budgets, so a waiting hook still sees the gate free within them.
WRITER_RETRY_CAP_SECONDS = 0.05


ADOPTION_VALIDATION_CACHE: set[tuple[object, ...]] = set()


# When a busy canonical release is tried again: soon, then once a minute for about
# an hour (audit 2026-09-26 A-13,
# docs/research/2026-09-26-a-busy-release-is-retried-until-it-lands.md).
RELEASE_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0) + (60.0,) * 60
# The one transaction target ceiling every writer that hands a page to the coordinator
# shares: the transaction contract, separate from the 8 MiB ordinary page reader
# (docs/research/2026-10-01-archive-pages-use-the-transaction-budget.md).
MAX_KNOWLEDGE_TARGET_BYTES = 64 * 1024 * 1024

# SQLite stores INTEGER as a signed 64-bit value; nothing larger reaches a column.
MAX_SQLITE_INT64 = (1 << 63) - 1


UINT64_MODULUS = 1 << 64


# How long a settled transaction keeps its before and after images.
#
# It was thirty days in four separate places, and nothing called `prune`, so on
# this vault it had never run: 11 369 transactions, 4.9 GB, half of it exact
# duplicates, for a journal that grows one line at a time. Measured 2026-09-02.
#
# Databases keep undo data only until the write settles — Oracle's
# `UNDO_RETENTION` is a minimum in seconds and its segments are reusable, and
# PostgreSQL's autovacuum drops old row versions as soon as no transaction needs
# them. Thirty days of images was not crash safety, it was an ad-hoc backup, and
# a backup is what replaces it: the owner accepted that trade on 2026-09-02,
# exchanging point-undo of any committed write within a month for a daily
# snapshot plus a copy off this machine.
#
# Two days rather than zero, for two reasons. A crash that spans midnight can
# still be unwound before the first snapshot exists; and a floor of one makes
# "shorter than the window" indistinguishable from "not a valid number of days",
# which collapses two different refusals into one message.
# See `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`.
UNDO_RETENTION_DAYS = 2


def encode_filesystem_id(value: object) -> int:
    """Map an unsigned filesystem ID bijectively onto SQLite's signed int64."""
    if type(value) is not int or not 0 <= value < UINT64_MODULUS:
        raise ValueError("filesystem identity must be an unsigned 64-bit integer")
    return value if value <= MAX_SQLITE_INT64 else value - UINT64_MODULUS


def encode_parent_identity(identity: tuple[object, object]) -> tuple[int, int]:
    return (encode_filesystem_id(identity[0]), encode_filesystem_id(identity[1]))


# A process number is handed out again after its process ends, so the writer of a
# `preparing` row is named by its number and its start together. The coordinator
# schema is compared text for text and pinned in the adoption record, so the start
# identity lives beside the attempt's other evidence instead of in a column. See
# `docs/research/2026-09-17-the-writer-of-an-unfinished-attempt-is-named-by-its-start.md`.
PREPARER_RECORD = "owner.json"
_MAX_PREPARER_RECORD_BYTES = 4096


def _recorded_preparer(artifact_root: Path) -> tuple[int, str] | None:
    """The writer an attempt recorded; None for a row older than the record."""
    try:
        with open(artifact_root / PREPARER_RECORD, "rb") as handle:
            raw = handle.read(_MAX_PREPARER_RECORD_BYTES + 1)
        value = json.loads(raw)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    pid, start = value.get("pid"), value.get("start_identity")
    if not _preparer_fields_valid(pid, start):
        return None
    return pid, start


def _preparer_fields_valid(pid: object, start: object) -> bool:
    if type(pid) is not int or not isinstance(start, str):
        return False
    return start.isascii() and 0 < len(start) <= 512


def preparer_alive(artifact_root: Path, owner_pid: object) -> bool:
    """Whether the process that wrote a `preparing` row still runs; doubt is alive."""
    if owner_pid is None:
        return False
    recorded = _recorded_preparer(artifact_root)
    if recorded is None or recorded[0] != owner_pid:
        return process_liveness.owner_alive(owner_pid)
    from operational_ownership import ProcessIdentity, process_identity_state

    identity = ProcessIdentity(pid=recorded[0], start_identity=recorded[1])
    return process_identity_state(identity) != "dead"


# Every write stores a full copy of the target before and a full copy after, and
# the targets are append-only journals — so appending 1 KB to a 500 KB journal
# costs 1 MB. Measured 2026-09-05: `run/transactions` had reached **5.3 GB** in
# 6904 records of median 700 KB, none of it prunable, because retention was
# already narrowed to two days and the size of a record had never been touched.
#
# Sampling 1412 images: 95% distinct, so deduplication no longer pays; the bytes
# compress to 9.7% with `lzma` at preset 1, 0.21 s for 7.7 MB. `compression.zstd`
# is available since the move to Python 3.14 but has not been measured on these
# images; adopt it only after such a measurement, and keep reading lzma images.
#
# The recorded `sha256` is always the hash of the **plaintext**, so every
# verification, rollback and abort path checks exactly what it checked before.
# Images written by an older build stay readable: only a file that begins with
# the lzma magic is decompressed, so nothing has to be migrated.
# See `docs/research/2026-09-05-an-undo-trail-that-outgrew-its-purpose.md`.
LZMA_MAGIC = b"\xfd7zXZ\x00"


def image_bytes(path: Path) -> bytes:
    """One staged image as the bytes it stands for, compressed or not."""
    with _open_image_stream(path) as stream:
        return stream.read()


def image_digest(path: Path) -> str:
    """Hash every decoded byte, preserving historical plain and XZ images."""
    with _open_image_stream(path) as stream:
        return _image_stream_digest(stream)


@contextlib.contextmanager
def _open_image_stream(path):
    """Own both encoded input and decoder, preserving historical error categories."""
    import lzma

    try:
        with path.open("rb") as raw:
            yield from _plain_or_decoded_stream(raw)
    except EOFError as exc:
        raise lzma.LZMAError(str(exc)) from exc


def _plain_or_decoded_stream(raw):
    import lzma

    prefix = raw.read(len(LZMA_MAGIC))
    raw.seek(0)
    if prefix != LZMA_MAGIC:
        yield raw
        return
    with lzma.LZMAFile(raw, "rb") as decoded:
        yield decoded


def _image_stream_digest(stream):
    digest = hashlib.sha256()
    for chunk in iter(functools.partial(stream.read, IO_CHUNK_BYTES), b""):
        digest.update(chunk)
    return digest.hexdigest()


def content_digest_text(content: bytes | VerifiedByteRange | None) -> str:
    if content is None:
        return ABSENT
    if isinstance(content, VerifiedByteRange):
        return content.range_digest(0, len(content))
    return sha256_bytes(content)


_PREFIX_STATE = frozenset({"sha256", "length"})


def plan_schema(plan: object) -> Path:
    """The schema of the plan's own version; an unknown one is refused by the v2 const."""
    version = plan.get("schema_version") if isinstance(plan, dict) else None
    return _PLAN_SCHEMAS.get(version, PLAN_SCHEMA)


def is_target_boundary_error(error: BaseException) -> bool:
    """A containment failure is named by its type; a bare ENOENT is not one.

    See `docs/research/2026-09-18-a-boundary-failure-is-a-type-not-a-phrase.md`.
    """
    return isinstance(error, (TargetBoundaryFailure, TargetPathBoundaryError))


UNAVAILABLE_STATE = object()


def inverse_change_kind(before_hash: str, after_hash: str) -> str:
    """Undoing a create is a delete, and undoing a delete is a create."""
    if before_hash == ABSENT:
        return "delete"
    if after_hash == ABSENT:
        return "create"
    return "replace"


def _before_artifact(root: Path, transaction_id: str, position: object) -> Path:
    return root / transaction_id / "before" / f"{int(position):06d}.bin"


def stored_before_state(root: Path, transaction_id: str, row: sqlite3.Row) -> object:
    """The undo image for one operation; ABSENT when there was nothing there.

    An append keeps no before-image, only the before length in its plan (v2); the
    bytes are rebuilt from the target when the undo is applied, and hashed there.
    """
    if row["before_hash"] == ABSENT:
        return ABSENT
    artifact = _before_artifact(root, transaction_id, row["position"])
    if not artifact.exists():
        return _recorded_before_length(root, transaction_id, row)
    return verified_before_image(artifact, row)


def _recorded_before_length(root: Path, transaction_id: str, row: sqlite3.Row) -> dict[str, object]:
    """An append's before state from its plan; RuntimeError when the plan holds none."""
    try:
        plan = json.loads((root / transaction_id / "plan.json").read_bytes())
        state = plan["operations"][int(row["position"])]["before"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"transaction before state is unreadable for {row['path']}") from exc
    if not isinstance(state, dict) or frozenset(state) != _PREFIX_STATE or state["sha256"] != row["before_hash"]:
        raise RuntimeError(f"transaction before state is not recorded for {row['path']}")
    return state


def verified_before_image(artifact: Path, row: sqlite3.Row) -> dict[str, object]:
    if image_digest(artifact) != row["before_hash"]:
        raise RefusedOperation(
            f"transaction before-image is corrupt for {row['path']}",
            "before_image_corrupt",
        )
    return {
        "sha256": row["before_hash"],
        "artifact": f"before/{int(row['position']):06d}.bin",
    }


def optional_before_state(
    root: Path, transaction_id: str, row: sqlite3.Row
) -> object:
    """UNAVAILABLE_STATE means the undo image is missing or corrupt, so skip it."""
    try:
        return stored_before_state(root, transaction_id, row)
    except (OSError, RuntimeError):
        return UNAVAILABLE_STATE


CONFLICTED_STATE_CODES = {
    "after": "unknown_target_bytes",
    "before": "before_hash_mismatch",
}


def retry_writer_or_raise(exc: BaseException, attempt: int, deadline: float) -> int:
    """Wait out transient contention; anything else belongs to the caller."""
    if not is_transient_writer_contention(exc):
        raise exc
    delay = writer_retry_delay(attempt, deadline)
    if delay <= 0:
        raise exc
    time.sleep(delay)
    return attempt + 1


def lstat_or_none(target: Path) -> os.stat_result | None:
    try:
        return os.lstat(target)
    except FileNotFoundError:
        return None


def is_transient_writer_contention(error: BaseException) -> bool:
    if isinstance(error, sqlite3.OperationalError):
        return busy_database(error)
    if isinstance(error, OSError):
        return _windows_sharing_violation(error)
    return False


def _windows_sharing_violation(error: OSError) -> bool:
    return getattr(error, "winerror", None) in {32, 33} or error.errno in {32, 33}


def writer_retry_delay(attempt: int, deadline: float) -> float:
    delay = min(
        _WRITER_RETRY_BASE_SECONDS * (2 ** min(attempt, 8)),
        WRITER_RETRY_CAP_SECONDS,
    )
    return max(0.0, min(delay, deadline - time.monotonic()))


def utc_now() -> datetime:
    return datetime.now(UTC)


def now() -> str:
    return utc_text(datetime.now(UTC))


def future_timestamp(seconds: float) -> str:
    return utc_text(datetime.now(UTC) + timedelta(seconds=seconds))


def stored_timestamp_text(value: datetime) -> str:
    return utc_text(value)


def parse_stored_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        harden_windows_acl(path)
    else:
        set_owner_only(path, mode)


def close_windows_handle(handle: int) -> None:
    if not windows_kernel32().CloseHandle(handle):
        raise OSError(ctypes.get_last_error(), "cannot close Windows directory handle")
