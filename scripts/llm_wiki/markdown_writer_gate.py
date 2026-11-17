"""The Markdown writer gate: legacy and canonical leases, heartbeats and nesting."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from operational_ownership import OwnerLease
import process_liveness
from llm_wiki import markdown_store
from llm_wiki.coordinator_schema import (
    COORDINATOR_V3_CONTRACT,
)
from llm_wiki.markdown_records import (
    WriterAdmissionTimeout,
)
from llm_wiki.markdown_store import (
    future_timestamp,
    is_transient_writer_contention,
    parse_stored_timestamp,
    retry_writer_or_raise,
    stored_timestamp_text,
    writer_retry_delay,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_transactions import begin_immediate

# Nested gate releases that failed in this process: (database, owner token, fencing epoch).
# Set operations are atomic under the interpreter lock; an entry is proof the row is residue.
UNRELEASED_PROJECTIONS: set[tuple[str, str, int]] = set()


def _require_gate_wait(wait_seconds: object) -> None:
    if wait_seconds is None:
        return
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or wait_seconds < 0
    ):
        raise ValueError("writer gate wait_seconds must be non-negative or None")


def _gate_wait_seconds(wait_seconds: float | None) -> float:
    if wait_seconds is None:
        return markdown_store.WRITER_WAIT_SECONDS
    return wait_seconds


def retry_gate_or_fail(
    exc: BaseException, attempt: int, deadline: float
) -> int:
    """The next attempt number, or the timeout that ends the wait."""
    if not is_transient_writer_contention(exc):
        raise exc
    delay = writer_retry_delay(attempt, deadline)
    if delay <= 0:
        raise WriterAdmissionTimeout(
            "timed out waiting for the global Markdown writer gate"
        ) from exc
    time.sleep(delay)
    return attempt + 1


def _require_wait_seconds(wait_seconds: object) -> None:
    if wait_seconds is None:
        return
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or wait_seconds < 0
    ):
        raise ValueError("writer gate wait_seconds must be non-negative or None")


def _writer_gate_busy() -> Exception:
    """The refusal the writer gate's wait window already knows how to treat."""
    from operational_ownership import OperationalOwnershipError

    return OperationalOwnershipError("owner_busy")


def writer_wait_window(wait_seconds: float | None) -> float:
    if wait_seconds is None:
        return markdown_store.WRITER_WAIT_SECONDS
    return wait_seconds


def _owns_writer_gate(
    row: object, owner_token: str, fencing_epoch: int
) -> bool:
    return bool(
        row is not None
        and row["owner_token"] == owner_token
        and row["fencing_epoch"] == fencing_epoch
    )


def _heartbeat_retry(
    exc: BaseException, attempt: int, lease_deadline: float, stop: threading.Event
) -> int | None:
    """None means the gate is lost: the wait ran out or the stop event fired."""
    if not is_transient_writer_contention(exc):
        return None
    delay = writer_retry_delay(attempt, lease_deadline)
    if delay <= 0 or stop.wait(delay):
        return None
    return attempt + 1


def _retried_heartbeat(
    exc: BaseException, attempt: int, lease_deadline: float, stop: threading.Event
) -> tuple[float, int] | None:
    retried = _heartbeat_retry(exc, attempt, lease_deadline, stop)
    if retried is None:
        return None
    return lease_deadline, retried


def _canonical_lease_retry_delay(
    exc: Exception, attempt: int, deadline: float
) -> float:
    if getattr(exc, "code", None) != "owner_busy":
        raise exc
    delay = writer_retry_delay(attempt, deadline)
    if delay <= 0:
        raise WriterAdmissionTimeout(
            "timed out waiting for the global Markdown writer gate"
        ) from exc
    return delay


def _nested_owner_retry_delay(exc: Exception, attempt: int, deadline: float) -> float:
    delay = writer_retry_delay(attempt, deadline)
    if getattr(exc, "code", None) != "owner_busy" or delay <= 0:
        raise exc
    return delay


def _writer_gate_loss_message(heartbeat_lost: bool) -> str:
    """Say what actually went wrong, since only a reclaim is a real loss."""
    if heartbeat_lost:
        return (
            "Markdown writer gate ownership was lost: the lease heartbeat stopped "
            "and another owner reclaimed the gate"
        )
    return "Markdown writer gate ownership was lost: another owner holds the gate"


def _row_start_identity(row: sqlite3.Row) -> str | None:
    """The owner's recorded start identity; a row of an older schema has none (C-12)."""
    return row["process_start_identity"] if "process_start_identity" in row.keys() else None


class WriterGateOperations:
    def _reentrant_writer_gate(self, depth: int) -> Iterator[OwnerLease]:
        self._local.gate_depth = depth + 1
        try:
            yield getattr(self._local, "gate_owner", None)
        finally:
            self._local.gate_depth -= 1

    def _install_legacy_owner(
        self, database: sqlite3.Connection, owner_token: str
    ) -> int:
        from operational_ownership import current_process_identity

        identity = current_process_identity()
        fence = database.execute(
            "SELECT last_epoch FROM writer_fences WHERE gate_name = 'global'"
        ).fetchone()
        fencing_epoch = 1 if fence is None else fence["last_epoch"] + 1
        database.execute(
            "INSERT INTO writer_fences (gate_name, last_epoch) VALUES ('global', ?) "
            "ON CONFLICT(gate_name) DO UPDATE SET last_epoch = excluded.last_epoch",
            (fencing_epoch,),
        )
        heartbeat = markdown_store.now()
        expires = future_timestamp(markdown_store.WRITER_LEASE_SECONDS)
        database.execute("DELETE FROM writer_owners WHERE gate_name = 'global'")
        database.execute(
            "INSERT INTO writer_owners "
            "(gate_name, owner_token, process_id, thread_id, acquired_at, "
            "heartbeat_at, expires_at, fencing_epoch, process_start_identity) "
            "VALUES ('global', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_token,
                identity.pid,
                threading.get_ident(),
                heartbeat,
                heartbeat,
                expires,
                fencing_epoch,
                identity.start_identity,
            ),
        )
        return fencing_epoch

    def _try_claim_legacy_gate(
        self, owner_token: str, deadline: float
    ) -> int | None:
        """The fencing epoch once claimed, or None while another owner holds it."""
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM writer_owners WHERE gate_name = 'global'"
            ).fetchone()
            if row is not None and not self._writer_owner_reclaimable(row):
                return None
            return self._install_legacy_owner(database, owner_token)

    def _acquire_legacy_gate(self, owner_token: str, deadline: float) -> int:
        acquisition_attempt = 0
        while True:
            try:
                fencing_epoch = self._try_claim_legacy_gate(owner_token, deadline)
            except (OSError, sqlite3.Error) as exc:
                acquisition_attempt = retry_gate_or_fail(
                    exc, acquisition_attempt, deadline
                )
                continue
            if fencing_epoch is not None:
                return fencing_epoch
            if time.monotonic() >= deadline:
                raise WriterAdmissionTimeout(
                    "timed out waiting for the global Markdown writer gate"
                )
            time.sleep(0.01)

    def _stop_legacy_gate(
        self,
        owner_token: str,
        fencing_epoch: int,
        heartbeat_stop: threading.Event,
        heartbeat_thread: threading.Thread,
        heartbeat_lost: threading.Event,
    ) -> None:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=markdown_store.WRITER_HEARTBEAT_SECONDS * 2)
        try:
            self._release_writer_gate(
                owner_token, fencing_epoch, heartbeat_lost.is_set()
            )
        finally:
            self._local.gate_depth = 0
            self._local.gate_token = None
            self._local.gate_fence = None

    def _legacy_writer_gate(
        self, wait_seconds: float | None
    ) -> Iterator[OwnerLease]:
        _require_gate_wait(wait_seconds)
        owner_token = uuid.uuid4().hex
        deadline = time.monotonic() + _gate_wait_seconds(wait_seconds)
        fencing_epoch = self._acquire_legacy_gate(owner_token, deadline)
        self._local.gate_depth = 1
        self._local.gate_token = owner_token
        self._local.gate_fence = fencing_epoch
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_writer_gate,
            args=(owner_token, fencing_epoch, heartbeat_stop, heartbeat_lost),
            name="markdown-writer-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield None
        finally:
            self._stop_legacy_gate(
                owner_token,
                fencing_epoch,
                heartbeat_stop,
                heartbeat_thread,
                heartbeat_lost,
            )

    @contextlib.contextmanager
    def writer_gate(
        self,
        *,
        owner: OwnerLease | None = None,
        wait_seconds: float | None = None,
    ) -> Iterator[OwnerLease]:
        if owner is not None:
            yield from self._nested_writer_gate(owner, wait_seconds)
            return
        depth = getattr(self._local, "gate_depth", 0)
        if depth:
            yield from self._reentrant_writer_gate(depth)
            return
        yield from self._contract_writer_gate(wait_seconds)

    def _contract_writer_gate(
        self, wait_seconds: float | None
    ) -> Iterator[OwnerLease]:
        """The canonical gate when this coordinator is v3, otherwise the legacy one."""
        if getattr(self, "_database_contract", None) == COORDINATOR_V3_CONTRACT:
            yield from self._canonical_writer_gate(wait_seconds)
            return
        yield from self._legacy_writer_gate(wait_seconds)

    def _canonical_writer_gate(
        self, wait_seconds: float | None
    ) -> Iterator[OwnerLease]:
        _require_wait_seconds(wait_seconds)
        registry = self._ownership_registry()
        lease = self._acquire_canonical_lease(registry, wait_seconds)
        self._enter_gate(lease)
        stop = threading.Event()
        lost = threading.Event()
        heartbeat = self._start_canonical_heartbeat(registry, lease, stop, lost)
        try:
            yield lease
        finally:
            self._leave_canonical_gate(registry, lease, stop, heartbeat)

    def _acquire_canonical_lease(
        self, registry: object, wait_seconds: float | None
    ) -> OwnerLease:
        deadline = time.monotonic() + writer_wait_window(wait_seconds)
        attempt = 0
        while True:
            try:
                return self._claim_canonical_lease(registry)
            except Exception as exc:
                delay = _canonical_lease_retry_delay(exc, attempt, deadline)
            time.sleep(delay)
            attempt += 1

    def _claim_canonical_lease(self, registry: object) -> OwnerLease:
        with self._connect() as database, begin_immediate(database):
            self._reclaim_dead_writer_projection(database, registry)
            lease = registry._acquire_in_transaction(
                database, "markdown-writer", scope="global"
            )
            self._insert_writer_projection(database, lease)
        return lease

    @staticmethod
    def _reclaim_dead_writer_projection(
        database: sqlite3.Connection, registry: object
    ) -> None:
        """Take over the one gate row when its owner is provably dead.

        `writer_owners.gate_name` is the primary key, so exactly one row can
        exist and a row left behind by an abrupt death blocks every later
        writer. The registry reclaims a dead *owner* by `(role, scope)`, and
        this row is keyed by neither: a nested gate records the project lease
        that entered it, so a dead project writer wedged the global gate for
        everybody, including the generation publication.

        Measured on the live vault 2026-08-28: one row for `gate_name`
        `global`, canonical owner `project:<one project>`, lease expired at
        20:50:21Z, pid 2095087 gone — and every generation pass since answered
        `sqlite3.IntegrityError: UNIQUE constraint failed:
        writer_owners.gate_name`, twice in a row on an otherwise idle machine.

        The proof is the registry's own `reclaimable_dead_owner`, not a second
        answer to the same question, and doubt refuses: a row that cannot be
        proved dead raises `owner_busy`, which the caller's wait window
        already knows how to treat. That also turns a live nested writer from
        an unhandled IntegrityError into the refusal it always meant.
        """
        row = database.execute(
            "SELECT * FROM writer_owners WHERE gate_name='global'"
        ).fetchone()
        if row is None:
            return
        if not registry.reclaimable_dead_owner(row):
            raise _writer_gate_busy()
        database.execute(
            "DELETE FROM writer_owners WHERE gate_name='global' AND owner_token=?",
            (row["owner_token"],),
        )

    def _enter_gate(self, lease: OwnerLease) -> None:
        self._local.gate_depth = 1
        self._local.gate_token = lease.token
        self._local.gate_fence = lease.epoch
        self._local.gate_owner = lease

    def _clear_gate(self) -> None:
        self._local.gate_depth = 0
        self._local.gate_token = None
        self._local.gate_fence = None
        self._local.gate_owner = None

    def _start_canonical_heartbeat(
        self,
        registry: object,
        lease: OwnerLease,
        stop: threading.Event,
        lost: threading.Event,
    ) -> threading.Thread:
        # The gate is already taken; its expiry is counted from here.
        heartbeat = threading.Thread(
            target=self._heartbeat_canonical_writer_gate,
            args=(registry, lease, stop, lost, time.monotonic()),
            name="markdown-writer-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        return heartbeat

    def _leave_canonical_gate(
        self,
        registry: object,
        lease: OwnerLease,
        stop: threading.Event,
        heartbeat: threading.Thread,
    ) -> None:
        stop.set()
        heartbeat.join(timeout=lease.heartbeat_seconds * 2)
        try:
            self._release_canonical_lease(registry, lease)
        except (OSError, sqlite3.Error) as exc:
            if not is_transient_writer_contention(exc):
                raise
            self._release_later(registry, lease)
        finally:
            self._clear_gate()

    def _release_canonical_lease(self, registry: object, lease: OwnerLease) -> None:
        # A heartbeat that failed transiently is not a lost gate. What
        # proves the loss is the projection row: reclaiming it deletes
        # this owner's row and bumps the fence, so a delete that removes
        # our row means nobody else ever took the gate.
        with self._connect() as database, begin_immediate(database):
            self._delete_writer_projection(database, lease)
            registry._release_in_transaction(database, lease)

    def _release_later(self, registry: object, lease: OwnerLease) -> None:
        """Keep trying a release the busy database refused; nobody else may take our row.

        The registry reclaims only a dead owner, and this process is alive, so a
        release left undone shut every other writer out until it exited (A-13).
        """
        threading.Thread(
            target=self._retry_release,
            args=(registry, lease),
            name="markdown-writer-release",
            daemon=True,
        ).start()

    def _retry_release(self, registry: object, lease: OwnerLease) -> None:
        for delay in markdown_store.RELEASE_RETRY_DELAYS:
            time.sleep(delay)
            if self._release_settled(registry, lease):
                return

    def _release_settled(self, registry: object, lease: OwnerLease) -> bool:
        """True once released, or once the failure is not contention and waiting cannot help."""
        try:
            self._release_canonical_lease(registry, lease)
        except (OSError, sqlite3.Error) as exc:
            return not is_transient_writer_contention(exc)
        return True

    @staticmethod
    def _insert_writer_projection(database: sqlite3.Connection, owner: OwnerLease) -> None:
        now = stored_timestamp_text(owner.heartbeat_at)
        database.execute(
            """INSERT INTO writer_owners(
                   gate_name,owner_token,process_id,thread_id,acquired_at,
                   heartbeat_at,expires_at,fencing_epoch,canonical_role,
                   canonical_scope,actor_id,process_start_identity
               ) VALUES ('global',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                owner.token,
                owner.process.pid,
                threading.get_ident(),
                stored_timestamp_text(owner.acquired_at),
                now,
                stored_timestamp_text(owner.expires_at),
                owner.epoch,
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.process.start_identity,
            ),
        )

    @staticmethod
    def _delete_writer_projection(
        database: sqlite3.Connection, owner: OwnerLease
    ) -> None:
        deleted = database.execute(
            """DELETE FROM writer_owners
               WHERE gate_name='global' AND owner_token=? AND fencing_epoch=?
                 AND canonical_role=? AND canonical_scope=? AND actor_id=?
                 AND process_id=? AND process_start_identity=?""",
            (
                owner.token,
                owner.epoch,
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.process.pid,
                owner.process.start_identity,
            ),
        ).rowcount
        if deleted != 1:
            raise RuntimeError(_writer_gate_loss_message(heartbeat_lost=False))

    def _heartbeat_canonical_writer_gate(
        self,
        registry: object,
        owner: OwnerLease,
        stop: threading.Event,
        lost: threading.Event,
        held_since: float,
    ) -> None:
        """Renew the gate and its projection; a busy database is not a lost gate.

        See `docs/research/2026-09-14-a-busy-database-is-not-a-lost-lease.md`.
        """
        from lease_renewal import renew_until_stopped

        ended = renew_until_stopped(
            lambda: self._renew_canonical_writer_gate(registry, owner),
            interval=owner.heartbeat_seconds,
            lease_seconds=owner.ttl_seconds,
            attempt_seconds=DEFAULTS.markdown_busy_ms / 1_000,
            held_since=held_since,
            stop=stop,
        )
        if ended is not None:
            lost.set()

    def _renew_canonical_writer_gate(self, registry: object, owner: OwnerLease) -> None:
        with self._connect() as database, begin_immediate(database):
            renewed = registry._heartbeat_in_transaction(database, owner)
            updated = database.execute(
                """UPDATE writer_owners SET heartbeat_at=?,expires_at=?
                   WHERE gate_name='global' AND owner_token=?
                     AND fencing_epoch=?""",
                (
                    stored_timestamp_text(renewed.heartbeat_at),
                    stored_timestamp_text(renewed.expires_at),
                    owner.token,
                    owner.epoch,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeError("writer projection was lost")

    def _require_nested_gate_owner(self, owner: object) -> None:
        """The two refusals a nested gate makes before it touches any state."""
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        if getattr(self, "_database_contract", None) != COORDINATOR_V3_CONTRACT:
            raise RuntimeError("canonical writer projection requires a v3 coordinator")

    def _nested_writer_gate(
        self, owner: OwnerLease, wait_seconds: float | None = None
    ) -> Iterator[OwnerLease]:
        self._require_nested_gate_owner(owner)
        _require_wait_seconds(wait_seconds)
        if getattr(self._local, "gate_depth", 0):
            yield from self._reentered_writer_gate(owner)
            return

        self._project_nested_owner(owner, wait_seconds)
        self._enter_gate(owner)
        try:
            yield owner
        finally:
            self._leave_nested_gate(owner)

    def _project_nested_owner(
        self, owner: OwnerLease, wait_seconds: float | None
    ) -> None:
        """Take the gate row for this owner, waiting out a live writer as the canonical gate does.

        The wait used to be ignored: one attempt, then `owner_busy`, so a project
        checkpoint that met a running compile failed at once. When the window
        closes the `owner_busy` it was given is what the caller sees.
        """
        from operational_ownership import OperationalOwnershipError

        deadline = time.monotonic() + writer_wait_window(wait_seconds)
        attempt = 0
        while True:
            try:
                return self._project_nested_owner_once(owner)
            except OperationalOwnershipError as exc:
                delay = _nested_owner_retry_delay(exc, attempt, deadline)
            time.sleep(delay)
            attempt += 1

    def _project_nested_owner_once(self, owner: OwnerLease) -> None:
        registry = self._ownership_registry()
        with self._connect() as database, begin_immediate(database):
            registry.require(database, owner)
            self._drop_own_stale_projection(database, owner)
            self._reclaim_dead_writer_projection(database, registry)
            self._insert_writer_projection(database, owner)

    def _leave_nested_gate(self, owner: OwnerLease) -> None:
        """Delete the projection, and clear this thread's gate whatever the delete did.

        A delete that raised — `database is locked` after the busy timeout — used to leave
        the thread believing it was still inside the gate, and the row shut every other
        process out until this one exited. The row it may leave behind is removed on the
        next entry (`_drop_own_stale_projection`). Research:
        `docs/research/2026-09-17-a-failed-release-does-not-keep-the-gate.md`.
        """
        residue = (str(self.database_path), owner.token, owner.epoch)
        UNRELEASED_PROJECTIONS.add(residue)
        try:
            with self._connect() as database, begin_immediate(database):
                self._delete_writer_projection(database, owner)
            UNRELEASED_PROJECTIONS.discard(residue)
        finally:
            self._clear_gate()

    def _drop_own_stale_projection(self, database: sqlite3.Connection, owner: OwnerLease) -> None:
        """Remove the row a failed release of this very lease left behind.

        Only a release recorded as failed in this process is proof; a row that merely
        looks like ours may be a live gate held through another coordinator.
        """
        residue = (str(self.database_path), owner.token, owner.epoch)
        if residue not in UNRELEASED_PROJECTIONS:
            return
        database.execute(
            "DELETE FROM writer_owners WHERE gate_name='global' AND owner_token=? AND fencing_epoch=?",
            (owner.token, owner.epoch),
        )
        UNRELEASED_PROJECTIONS.discard(residue)

    def _reentered_writer_gate(self, owner: OwnerLease) -> Iterator[OwnerLease]:
        """A nested gate belongs to the same owner or it is not the same gate."""
        if getattr(self._local, "gate_owner", None) != owner:
            raise RuntimeError("nested writer owner changed")
        self._local.gate_depth = getattr(self._local, "gate_depth", 0) + 1
        try:
            yield owner
        finally:
            self._local.gate_depth -= 1

    def _release_writer_gate(
        self, owner_token: str, fencing_epoch: int, heartbeat_lost: bool
    ) -> None:
        deadline = time.monotonic() + markdown_store.WRITER_WAIT_SECONDS
        attempt = 0
        while True:
            try:
                deleted = self._delete_writer_owner(owner_token, fencing_epoch, deadline)
            except (OSError, sqlite3.Error) as exc:
                attempt = retry_writer_or_raise(exc, attempt, deadline)
                continue
            if not deleted:
                raise RuntimeError(_writer_gate_loss_message(heartbeat_lost))
            return

    def _delete_writer_owner(
        self, owner_token: str, fencing_epoch: int, deadline: float
    ) -> bool:
        """False means somebody else holds the gate, so nothing was deleted."""
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(
            database
        ):
            row = database.execute(
                "SELECT owner_token, fencing_epoch FROM writer_owners "
                "WHERE gate_name = 'global'"
            ).fetchone()
            if not _owns_writer_gate(row, owner_token, fencing_epoch):
                return False
            database.execute(
                "DELETE FROM writer_owners WHERE gate_name = 'global' "
                "AND owner_token = ? AND fencing_epoch = ?",
                (owner_token, fencing_epoch),
            )
        return True

    def _writer_owner_reclaimable(self, row: sqlite3.Row) -> bool:
        expires_at = row["expires_at"]
        expired = not expires_at or parse_stored_timestamp(expires_at) <= datetime.now(UTC)
        return expired or not process_liveness.owner_alive(row["process_id"], _row_start_identity(row))

    def _heartbeat_writer_gate(
        self,
        owner_token: str,
        fencing_epoch: int,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        lease_deadline = time.monotonic() + markdown_store.WRITER_LEASE_SECONDS
        attempt = 0
        while not stop.wait(markdown_store.WRITER_HEARTBEAT_SECONDS):
            beat = self._heartbeat_once(
                owner_token, fencing_epoch, lease_deadline, attempt, stop
            )
            if beat is None:
                lost.set()
                return
            lease_deadline, attempt = beat

    def _heartbeat_once(
        self,
        owner_token: str,
        fencing_epoch: int,
        lease_deadline: float,
        attempt: int,
        stop: threading.Event,
    ) -> tuple[float, int] | None:
        """The next (lease deadline, attempt), or None when the gate is lost."""
        try:
            refreshed = self._refresh_writer_owner(
                owner_token, fencing_epoch, lease_deadline
            )
        except (OSError, sqlite3.Error) as exc:
            return _retried_heartbeat(exc, attempt, lease_deadline, stop)
        if not refreshed:
            return None
        return time.monotonic() + markdown_store.WRITER_LEASE_SECONDS, 0

    def _refresh_writer_owner(
        self, owner_token: str, fencing_epoch: int, lease_deadline: float
    ) -> bool:
        """False means the row is gone: this owner no longer holds the gate."""
        remaining = min(
            markdown_store.WRITER_HEARTBEAT_SECONDS, lease_deadline - time.monotonic()
        )
        remaining_ms = max(1, int(remaining * 1_000))
        with self._connect(busy_ms=remaining_ms) as database, begin_immediate(
            database
        ):
            cursor = database.execute(
                "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
                "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ?",
                (
                    markdown_store.now(),
                    future_timestamp(markdown_store.WRITER_LEASE_SECONDS),
                    owner_token,
                    fencing_epoch,
                ),
            )
            return cursor.rowcount == 1

    def writer_gate_held(self) -> bool:
        return bool(getattr(self._local, "gate_depth", 0))

    def assert_external_work_allowed(self) -> None:
        if self.writer_gate_held():
            raise RuntimeError("external LLM or Git work is forbidden under the writer gate")

    def coherent_read(self, paths: Sequence[Path]) -> dict[Path, bytes | None]:
        with self.writer_gate():
            return {Path(path): self._read_target(self._target(Path(path).as_posix())) for path in paths}
