"""Adoption is validated once per artifact identity before a writer opens the active coordinator."""
from __future__ import annotations

import contextlib
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from bounded_io import read_stable_bytes
from llm_wiki import adoption_admission, markdown_store, runtime_roots
from llm_wiki.coordinator_validation import reliability_v3_records_present
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_coordinator import MarkdownCoordinator
from llm_wiki.markdown_records import require_bytes
from llm_wiki.markdown_store import (
    LZMA_MAGIC,
    WRITER_RETRY_CAP_SECONDS,
    is_transient_writer_contention,
    writer_retry_delay,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active
from reliable_memory import read_runtime_bytes

# How long an admission check (a writer's, and doctor's) waits out a busy operational database. basis unknown — value predates measurement (one check takes about 0.36 s on the live vault, 2026-09-28); review when adoption reports this deadline on a real vault.
_ADOPTION_VALIDATION_SECONDS = 30.0


ADOPTION_VALIDATION_LOCK = threading.Lock()


def decoded_image_bytes(raw: bytes, *, max_bytes: int | None = None) -> bytes:
    """Decode the shared image format; optional budget bounds decoded evidence."""
    if not raw.startswith(LZMA_MAGIC):
        return _image_within_budget(raw, max_bytes)
    return _decompressed_image_bytes(raw, max_bytes)


def _decompressed_image_bytes(raw: bytes, max_bytes: int | None) -> bytes:
    import io
    import lzma

    if max_bytes is None:
        return lzma.decompress(raw)
    with lzma.LZMAFile(io.BytesIO(raw)) as stream:
        decoded = stream.read(max_bytes + 1)
    return _image_within_budget(decoded, max_bytes)


def _image_within_budget(raw: bytes, max_bytes: int | None) -> bytes:
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("decoded transaction image exceeds evidence budget")
    return raw


def recover_initial_contention(
    coordinator: MarkdownCoordinator,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> None:
    deadline = min(deadline, time.monotonic() + markdown_store.WRITER_WAIT_SECONDS)
    attempt = 0
    while not _recovered_or_waited(coordinator, attempt, deadline, cancelled):
        attempt += 1


def _recovered_or_waited(
    coordinator: MarkdownCoordinator,
    attempt: int,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> bool:
    """True once recovery succeeded; False after sleeping out transient contention."""
    try:
        coordinator.recover(
            writer_wait_seconds=max(0.0, deadline - time.monotonic()),
            deadline=deadline,
            cancelled=cancelled,
        )
        return True
    except (OSError, sqlite3.Error) as exc:
        delay = _contention_retry_delay(exc, attempt, deadline)
        if delay is None:
            raise
        time.sleep(delay)
    return False


def _contention_retry_delay(exc: BaseException, attempt: int, deadline: float) -> float | None:
    if not is_transient_writer_contention(exc):
        return None
    delay = writer_retry_delay(attempt, deadline)
    if delay <= 0:
        return None
    return delay


def stable_operation_id(kind: str, event_id: str, content: bytes) -> str:
    """Return a deterministic operation ID bound to one redacted event payload."""
    if not kind or not event_id:
        raise ValueError("operation kind and event_id must be non-empty")
    content = require_bytes(content)
    return f"{kind}:{event_id}:{sha256_bytes(content)}"


def _adoption_record_digest(path: Path, state_root: Path, deadline: float | None) -> str:
    operational_deadline_active(deadline)
    digest = sha256_bytes(read_runtime_bytes(path, state_root, max_bytes=1024 * 1024, owner_only=True))
    operational_deadline_active(deadline)
    return digest


def _adoption_validation_key(vault: Path, state_root: Path, *, deadline: float | None = None) -> tuple[object, ...]:
    operational_deadline_active(deadline)
    run_root = Path(state_root) / "run"
    records = tuple(
        _adoption_record_digest(path, state_root, deadline)
        for path in (
            run_root / "reliability-v3-migration.json",
            run_root / "reliability-v3-adopted.json",
        )
    )
    integration = read_stable_bytes(
        Path(vault) / "scripts" / "integration_adapter.py",
        16 * 1024 * 1024,
        label="installed integration adapter",
    )
    operational_deadline_active(deadline)
    identities = tuple(_active_database_identity(run_root / name) for name in ("queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"))
    operational_deadline_active(deadline)
    return (str(vault), str(state_root), *records, sha256_bytes(integration), *identities)


def _active_database_identity(path: Path) -> tuple[int, int, int]:
    active = os.lstat(path)
    return active.st_mode, active.st_dev, active.st_ino


def transient_adoption_contention(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if is_transient_writer_contention(current):
            return True
        current = current.__cause__
    return False


def retire_strays_before_validation(state_root: Path, *, deadline: float | None = None) -> None:
    """A provably stray candidate is moved aside here, where it would refuse.

    Only when a candidate exists: two `lstat`s otherwise. A candidate that holds
    data, a live owner or an adoption in flight stays, and the validation below
    refuses exactly as it did. See
    `docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md`.
    """
    operational_deadline_active(deadline)
    run = Path(state_root) / "run"
    if not any(os.path.lexists(run / name) for name in adoption_admission.STRAY_CANDIDATE_NAMES):
        return
    adoption_admission.retire_stray_candidates(state_root, datetime.now(UTC), deadline=deadline)


def _validate_adoption_with_retry(vault: Path, state_root: Path, *, deadline: float | None = None) -> None:
    retire_strays_before_validation(state_root, deadline=deadline)
    retry_adoption_validation(vault, state_root, adoption_admission.require_reliability_v3_admission, deadline=deadline)


def require_adopted_through_contention(vault: Path, state_root: Path, *, deadline: float | None = None) -> None:
    """Doctor's complete adoption certification, with ordinary contention retried."""
    retry_adoption_validation(vault, state_root, adoption_admission.require_reliability_v3_adopted, deadline=deadline)


def _adoption_retry_pause(error: Exception, deadline: float) -> None:
    if not transient_adoption_contention(error):
        raise error
    operational_deadline_active(deadline)
    time.sleep(min(WRITER_RETRY_CAP_SECONDS, max(0.0, deadline - time.monotonic())))


def _invoke_adoption_validator(validate, vault: Path, state_root: Path, deadline: float | None) -> None:
    if deadline is None:
        validate(root=vault, state_root=state_root)
        return
    validate(root=vault, state_root=state_root, deadline=deadline)


def retry_adoption_validation(vault: Path, state_root: Path, validate, *, deadline: float | None = None) -> None:
    operational_deadline_active(deadline)
    retry_deadline = time.monotonic() + _ADOPTION_VALIDATION_SECONDS
    if deadline is not None:
        retry_deadline = min(retry_deadline, deadline)
    while True:
        try:
            _invoke_adoption_validator(validate, vault, state_root, deadline)
            operational_deadline_active(deadline)
            return
        except Exception as exc:
            _adoption_retry_pause(exc, retry_deadline)


def _acquire_adoption_validation_lock(deadline: float | None) -> None:
    operational_deadline_active(deadline)
    if deadline is None or math.isinf(deadline):
        ADOPTION_VALIDATION_LOCK.acquire()
        return
    if not ADOPTION_VALIDATION_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("adoption validation mutex deadline expired")


@contextlib.contextmanager
def _adoption_validation_guard(deadline: float | None) -> Iterator[None]:
    _acquire_adoption_validation_lock(deadline)
    try:
        operational_deadline_active(deadline)
        yield
    finally:
        ADOPTION_VALIDATION_LOCK.release()


def require_adopted_once(vault: Path, state_root: Path, *, deadline: float | None = None) -> None:
    operational_deadline_active(deadline)
    key = _adoption_validation_key(vault, state_root, deadline=deadline)
    with _adoption_validation_guard(deadline):
        if key in markdown_store.ADOPTION_VALIDATION_CACHE:
            return
    _validate_adoption_with_retry(vault, state_root, deadline=deadline)
    with _adoption_validation_guard(deadline):
        markdown_store.ADOPTION_VALIDATION_CACHE.add(key)


def active_markdown_coordinator(vault: Path, state_root: Path, *, deadline: float | None = None) -> MarkdownCoordinator:
    """Open the validated adopted coordinator-v3 database for normal writes."""
    resolved_vault = Path(vault).resolve(strict=True)
    state = Path(state_root).absolute()
    require_adopted_once(resolved_vault, state, deadline=deadline)
    path = state / "run" / "markdown-transactions-v3.sqlite3"
    coordinator = MarkdownCoordinator.from_v3_active(path, state_root=state, deadline=deadline)
    coordinator.vault = resolved_vault
    return coordinator


def active_or_legacy_coordinator(
    vault: Path, state_root: Path, *, deadline: float | None = None
) -> MarkdownCoordinator:
    """The adopted V3 coordinator where adoption is in force, else the legacy one.

    Adoption replaces the pre-adoption `run/markdown-transactions.sqlite3` with a
    JSON tombstone, so a writer that opens that path directly fails with
    `file is not a database` on every call. This is the one rule that decides
    which coordinator a writer gets; a writer that constructs one itself
    bypasses adoption and dies on the tombstone.
    """
    operational_deadline_active(deadline)
    if reliability_v3_records_present(state_root):
        return active_markdown_coordinator(vault, state_root, deadline=deadline)
    return MarkdownCoordinator(vault, state_root, deadline=deadline)


def default_coordinator() -> MarkdownCoordinator:
    vault = runtime_roots.vault_root().resolve(strict=True)
    return active_or_legacy_coordinator(vault, runtime_roots.state_root())
