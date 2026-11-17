"""Publish capture intents and queue tasks under the writer fence."""
from __future__ import annotations

import stat
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from memory_state import MAX_CAPTURE_INTENT_BYTES

CAPTURE_HANDLER_VERSION = 1


def capture_relative_paths(intent_id: str) -> tuple[str, str]:
    shard = intent_id[:2]
    name = f"{intent_id}.json"
    pending = f"run/capture-intents/pending/{shard}/{name}"
    ready = f"run/capture-intents/ready/{shard}/{name}"
    return pending, ready


def validate_capture_directory(path: Path, state_root: Path) -> None:
    info = path.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    unsafe = (
        path.is_symlink(),
        bool(getattr(info, "st_file_attributes", 0) & reparse),
        not stat.S_ISDIR(info.st_mode),
    )
    if any(unsafe):
        raise PermissionError("capture intent directory is unsafe")
    path.resolve(strict=True).relative_to(state_root.resolve(strict=True))


def _ensure_capture_directory(path: Path, state_root: Path) -> None:
    from reliable_memory import fsync_directory, harden_runtime_owner_only

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        fsync_directory(path.parent)
    validate_capture_directory(path, state_root)
    harden_runtime_owner_only(path, 0o700)


def ensure_capture_intent_directories(state_root: Path, intent_id: str) -> None:
    base = state_root / "run" / "capture-intents"
    paths = (
        base,
        base / "pending",
        base / "ready",
        base / "pending" / intent_id[:2],
        base / "ready" / intent_id[:2],
    )
    for path in paths:
        _ensure_capture_directory(path, state_root)


def _remove_verified_pending(path: Path, state_root: Path, expected_sha256: str) -> None:
    from llm_wiki.formats.canonical import sha256_bytes
    from reliable_memory import fsync_directory, read_runtime_bytes

    try:
        payload = read_runtime_bytes(
            path, state_root, max_bytes=MAX_CAPTURE_INTENT_BYTES, owner_only=True
        )
    except FileNotFoundError:
        return
    if sha256_bytes(payload) != expected_sha256:
        raise RuntimeError("pending capture intent digest changed")
    path.unlink()
    fsync_directory(path.parent)


def publication_heartbeat_interval(owner) -> float:
    from llm_wiki.markdown_coordinator_base import INTENT_FENCE_SECONDS

    # Same policy as the existing capture worker: renew within one third of
    # the shortest authority lifetime, and respect the owner's own interval.
    return min(owner.heartbeat_seconds, INTENT_FENCE_SECONDS / 3)


class CapturePublicationKeepAlive:
    """Keep both publication claims live and verify them before returning."""

    def __init__(self, registry, coordinator, owner, fence, *, held_since: float) -> None:
        from llm_wiki.markdown_coordinator_base import INTENT_FENCE_SECONDS
        from llm_wiki.reliability_defaults import DEFAULTS

        self._registry, self._coordinator = registry, coordinator
        self._owner, self._fence = owner, fence
        self._interval = publication_heartbeat_interval(owner)
        self._lease_seconds = min(owner.ttl_seconds, INTENT_FENCE_SECONDS)
        self._attempt_seconds = 2 * DEFAULTS.markdown_busy_ms / 1_000
        self._held_since = held_since
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="capture-publication-keepalive", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        self._stop.set()
        # Exceptional exit also releases the outer authority scopes. Finish
        # any in-flight renewal before either exit path can release them.
        self._thread.join()
        if exc_type is None:
            self._require_finished()
            self._require_live()

    def _require_finished(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _require_live(self) -> None:
        from llm_wiki.markdown_coordinator_base import require_live_intent_fence

        with self._coordinator._connect() as database:
            self._registry.require(database, self._owner)
            require_live_intent_fence(database, self._fence, datetime.now(UTC))

    def _renew(self) -> None:
        self._owner = self._registry.heartbeat(self._owner)
        self._coordinator.heartbeat_intent_fence(self._fence, self._owner)

    def _run(self) -> None:
        from lease_renewal import renew_until_stopped

        self._failure = renew_until_stopped(
            self._renew, interval=self._interval, lease_seconds=self._lease_seconds,
            attempt_seconds=self._attempt_seconds, held_since=self._held_since, stop=self._stop,
        )


@contextmanager
def capture_publication_fence(queue: object, coordinator: object, intent_id: str):
    registry = queue.ownership_registry()
    owner = registry.acquire("capture", scope=f"intent:{intent_id}")
    held_since = time.monotonic()
    try:
        fence = coordinator.acquire_intent_fence(intent_id, mode="capture", owner=owner)
    except BaseException:
        registry.release(owner)
        raise
    try:
        with CapturePublicationKeepAlive(registry, coordinator, owner, fence, held_since=held_since):
            yield owner, fence
    finally:
        # A retained fence still depends on its canonical owner. If releasing
        # it fails, keep both for qualified recovery and preserve that error.
        coordinator.release_intent_fence(fence)
        registry.release(owner)


def _require_capture_registration_owner(coordinator, intent_id, owner, fence) -> None:
    from iso_time import utc_text

    coordinator._validate_intent_fence_owner(owner, "capture")
    if (fence.intent_id, fence.mode) != (intent_id, "capture"):
        raise ValueError("capture registration ownership has a different intent")
    registry = coordinator._ownership_registry()
    with coordinator._connect() as database:
        registry.require(database, owner)
        row = database.execute(
            """SELECT 1 FROM intent_fences
               WHERE intent_id=? AND mode='capture' AND token=? AND fencing_epoch=?
                 AND canonical_role=? AND canonical_scope=? AND canonical_actor_id=?
                 AND canonical_owner_token=? AND canonical_fencing_epoch=? AND expires_at>?""",
            (intent_id, fence.token, fence.epoch, owner.role, owner.scope,
             owner.actor_id, owner.token, owner.epoch, utc_text(datetime.now(UTC))),
        ).fetchone()
    if row is None:
        raise RuntimeError("intent_fence_lost")


@contextmanager
def _capture_registration_fence(queue, coordinator, intent_id, ownership):
    if ownership is None:
        with capture_publication_fence(queue, coordinator, intent_id) as held:
            yield held
        return
    if Path(queue.state_root).resolve() != Path(coordinator.state_root).resolve():
        raise ValueError("capture registration runtime roots differ")
    owner, fence = ownership
    _require_capture_registration_owner(coordinator, intent_id, owner, fence)
    yield owner, fence


def publish_capture_files_and_task(
    queue: object,
    coordinator: object,
    *,
    intent_id: str,
    payload: bytes,
    intent_sha256: str,
    pending_relative: str,
    ready_relative: str,
    handler_version: int = CAPTURE_HANDLER_VERSION,
    ownership: tuple[object, object] | None = None,
) -> None:
    from reliable_memory import publish_runtime_file

    state_root = Path(queue.state_root)
    pending = state_root / pending_relative
    ready = state_root / ready_relative
    with _capture_registration_fence(queue, coordinator, intent_id, ownership) as (owner, fence):
        publish_runtime_file(
            pending, payload, state_root=state_root, create_only=True, mode=0o600
        )
        queue.index_capture_intent_pending(
            intent_id=intent_id,
            pending_path=pending_relative,
            ready_path=ready_relative,
            intent_sha256=intent_sha256,
            byte_size=len(payload),
        )
        publish_runtime_file(
            ready, payload, state_root=state_root, create_only=True, mode=0o600
        )
        queue.mark_capture_intent_ready(
            intent_id=intent_id,
            pending_path=pending_relative,
            ready_path=ready_relative,
            intent_sha256=intent_sha256,
            byte_size=len(payload),
        )
        queue.enqueue_capture_task_replay_safe(
            "flush",
            handler_version,
            {
                "intent_id": intent_id,
                "intent_path": ready_relative,
                "intent_sha256": intent_sha256,
            },
            intent_id=intent_id,
            intent_path=ready_relative,
            intent_sha256=intent_sha256,
            capture_fence=fence,
            owner=owner,
        )
        _remove_verified_pending(pending, state_root, intent_sha256)
