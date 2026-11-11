"""Bounded lease and source-fence renewal threads; queue storage stays with callers."""
from __future__ import annotations

import threading
import time
from typing import Protocol

from llm_wiki.queue_contracts import QueueLease, QueueOperationError, SourceFence
from llm_wiki.reliability_defaults import DEFAULTS


class LeaseQueue(Protocol):
    def heartbeat(self, lease: QueueLease, *, lease_seconds: int) -> QueueLease: ...

    def _heartbeat_wait(self, stop: threading.Event, seconds: float) -> bool: ...


class SourceFenceQueue(Protocol):
    def heartbeat_source_fence(self, fence: SourceFence, *, lease_seconds: int) -> SourceFence: ...

    def _heartbeat_wait(self, stop: threading.Event, seconds: float) -> bool: ...


# A heartbeat thread may sit inside SQLite for `queue_busy_ms` (5 s); the
# join bound is twice the heartbeat with a floor above that wait, and a
# thread still alive past it is refused by name (audit OPS-18,
# docs/research/2026-09-11-a-join-has-a-bound-and-a-benchmark-does-not-grade-itself.md).
HEARTBEAT_STOP_FLOOR_SECONDS = 10.0


def _join_heartbeat_or_refuse(thread: threading.Thread, heartbeat_seconds: float) -> None:
    thread.join(timeout=max(2.0 * heartbeat_seconds, HEARTBEAT_STOP_FLOOR_SECONDS))
    if thread.is_alive():
        raise QueueOperationError("heartbeat_stop_timeout")


class SourceFenceHeartbeat:
    def __init__(
        self,
        queue: SourceFenceQueue,
        fence: SourceFence,
        *,
        heartbeat_seconds: int,
        lease_seconds: int,
    ) -> None:
        self._queue = queue
        self._fence = fence
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        # The fence row is already written; its expiry is counted from here.
        self._held_since = time.monotonic()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-source-fence-heartbeat-{fence.daily_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        _join_heartbeat_or_refuse(self._thread, self._heartbeat_seconds)

    def current(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            return self._fence

    def refresh(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            try:
                self._fence = self._queue.heartbeat_source_fence(
                    self._fence, lease_seconds=self._lease_seconds
                )
            except Exception as exc:
                self.error = exc
                self._stop.set()
                raise QueueOperationError("source_fence_lost") from exc
            return self._fence

    def _run(self) -> None:
        """A busy database is retried until the fence expires; a lost fence is final.

        See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
        """
        from lease_renewal import renew_until_stopped

        ended = renew_until_stopped(
            self._renew,
            interval=self._heartbeat_seconds,
            lease_seconds=self._lease_seconds,
            attempt_seconds=DEFAULTS.queue_busy_ms / 1_000,
            held_since=self._held_since,
            stop=self._stop,
            wait=lambda seconds: self._queue._heartbeat_wait(self._stop, seconds),  # noqa: SLF001
        )
        if ended is None:
            return
        with self._lock:
            self.error = ended
        self._stop.set()

    def _renew(self) -> None:
        with self._lock:
            self._fence = self._queue.heartbeat_source_fence(
                self._fence, lease_seconds=self._lease_seconds
            )


class LeaseHeartbeat:
    def __init__(
        self,
        queue: LeaseQueue,
        lease: QueueLease,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> None:
        self._queue = queue
        self._lease = lease
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        # The lease is already claimed; its expiry is counted from here.
        self._held_since = time.monotonic()
        self._stop = threading.Event()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-queue-heartbeat-{lease.id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        _join_heartbeat_or_refuse(self._thread, self._heartbeat_seconds)

    def _run(self) -> None:
        """A busy database is retried until the lease expires; completion stays fenced.

        See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
        """
        from lease_renewal import renew_until_stopped

        ended = renew_until_stopped(
            self._renew,
            interval=self._heartbeat_seconds,
            lease_seconds=self._lease_seconds,
            attempt_seconds=DEFAULTS.queue_busy_ms / 1_000,
            held_since=self._held_since,
            stop=self._stop,
            wait=lambda seconds: self._queue._heartbeat_wait(self._stop, seconds),  # noqa: SLF001
        )
        if ended is not None:
            self.error = ended
            self._stop.set()

    def _renew(self) -> None:
        self._lease = self._queue.heartbeat(self._lease, lease_seconds=self._lease_seconds)

