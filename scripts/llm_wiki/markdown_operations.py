"""Per-operation execution: forward application, publication and Windows paths."""

from __future__ import annotations

import contextlib
import ctypes
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from llm_wiki import markdown_store
from llm_wiki.coordinator_schema import COORDINATOR_V3_CONTRACT
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.markdown_records import (
    TargetBoundaryFailure,
    TargetStateMismatch,
    TransactionImageError,
    TransactionRecord,
)
from llm_wiki.markdown_store import ABSENT, close_windows_handle, future_timestamp, image_bytes
from llm_wiki.sqlite_transactions import begin_immediate
from llm_wiki.windows_native import (
    WindowsDispositionInformation,
    windows_kernel32,
)
from model_dlp import require_safe_publication
from reliable_memory import (
    durable_publish_file,
    fsync_directory,
)


def _link_or_replace(
    kind: str,
    temporary_name: str,
    name: str,
    parent_descriptor: int,
    path: str,
) -> None:
    """A create must not clobber; a replace must be atomic."""
    if kind != "create":
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        return
    try:
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise TargetStateMismatch("before", str(path)) from exc
    os.unlink(temporary_name, dir_fd=parent_descriptor)


def _remove_temporary_publication(temporary: Path) -> None:
    """Whatever happened, no half-written page is left beside the real one.

    A publish that succeeded has already moved the file, so this is a no-op
    there, and a failure to remove never replaces the error on its way up. See
    `docs/research/2026-09-18-nothing-half-written-is-left-behind.md`.
    """
    with contextlib.suppress(OSError):
        temporary.unlink(missing_ok=True)


def open_windows_file_for_mutation(path: Path) -> int:
    kernel32 = windows_kernel32()
    generic_read = 0x80000000
    delete_access = 0x00010000
    share_read = 0x1
    share_delete = 0x4
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        generic_read | delete_access,
        share_read | share_delete,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open Windows mutation handle: {path}")
    return handle


def delete_windows_handle(file_handle: int) -> None:
    information = WindowsDispositionInformation(True)
    file_disposition_info = 4
    kernel32 = windows_kernel32()
    if not kernel32.SetFileInformationByHandle(
        file_handle,
        file_disposition_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(ctypes.get_last_error(), "cannot delete Windows target")


_UNDO_KILLPOINT_ALIASES = {
    "after_each_target": "after_each_undo_target",
    "before_commit": "before_undo_commit",
}


def _killpoint_aliases(name: str, parent_transaction_id: str | None) -> set[str]:
    """An undo transaction answers to its own killpoint names as well."""
    aliases = {name, _undo_killpoint_alias(name, parent_transaction_id)}
    if parent_transaction_id is None:
        return aliases
    extra = _UNDO_KILLPOINT_ALIASES.get(name)
    if extra is not None:
        aliases.add(extra)
    return aliases


def _undo_killpoint_alias(name: str, parent_transaction_id: str | None) -> str:
    if not name.startswith("after_"):
        return name
    prefix = "undo_" if parent_transaction_id is not None else ""
    return f"{name[:6]}{prefix}{name[6:]}"


class OperationExecution:
    def _killpoint(self, name: str, parent_transaction_id: str | None = None) -> None:
        configured = os.environ.get("LLM_WIKI_TRANSACTION_KILLPOINT")
        if configured in _killpoint_aliases(name, parent_transaction_id):
            os._exit(86)

    def _reconcile_operation_states(
        self, transaction_id: str, rows: Sequence[sqlite3.Row]
    ) -> set[str]:
        reconciled_after: set[str] = set()
        for row in rows:
            if self._reconcile_one_operation(transaction_id, row):
                reconciled_after.add(row["path"])
        return reconciled_after

    def _reconcile_one_operation(self, transaction_id: str, row: sqlite3.Row) -> bool:
        """True when the target already holds this operation's after state."""
        current = self._operation_hash(row)
        if row["applied"]:
            if current != row["after_hash"]:
                raise TargetStateMismatch("after", str(row["path"]))
            return True
        return self._reconcile_unapplied_operation(transaction_id, row, current)

    def _reconcile_unapplied_operation(
        self, transaction_id: str, row: sqlite3.Row, current: str
    ) -> bool:
        """True when an unmarked operation already holds its after state."""
        if current == row["after_hash"]:
            self._mark_operation_applied(transaction_id, row["position"])
            return True
        if current != row["before_hash"]:
            raise TargetStateMismatch("before", str(row["path"]))
        return False

    def _mark_operation_applied(self, transaction_id: str, position: int) -> None:
        active_database = getattr(self._local, "mutation_database", None)
        if active_database is not None:
            active_database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )
            return
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            database.execute(
                'UPDATE "operation" SET applied = 1 '
                "WHERE transaction_id = ? AND position = ?",
                (transaction_id, position),
            )

    def _mutate_and_mark(
        self,
        transaction_id: str,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        *,
        content_guard: str | None = None,
    ) -> None:
        active_database = getattr(self._local, "mutation_database", None)
        if active_database is not None:
            self._assert_writer_ownership(active_database)
            self._apply_forward_operation(row, operation_plan, content_guard)
            self._require_operation_state(row, row["after_hash"], "after")
            self._mark_operation_applied(transaction_id, row["position"])
            return
        with self._connect() as database, begin_immediate(
            database, before_commit=self._require_current_operation_active
        ):
            self._assert_writer_ownership(database)
            self._local.mutation_database = database
            try:
                self._apply_forward_operation(row, operation_plan, content_guard)
                self._require_operation_state(row, row["after_hash"], "after")
                self._mark_operation_applied(transaction_id, row["position"])
            finally:
                self._local.mutation_database = None

    @staticmethod
    def _content_guard(
        record: TransactionRecord, plan: Mapping[str, object]
    ) -> str | None:
        configured = plan.get("content_guard")
        if configured == "model_output":
            return configured
        if record.operation_id.startswith(
            ("compile:", "compile-quarantine:", "contradiction:")
        ):
            return "model_output"
        return None

    def _apply_forward_operation(
        self,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        content_guard: str | None,
    ) -> None:
        previous = getattr(self._local, "content_guard", None)
        self._local.content_guard = content_guard
        try:
            self._apply_operation(row, operation_plan)
        finally:
            self._local.content_guard = previous

    def _refresh_v3_writer_lease(
        self,
        database: sqlite3.Connection,
        owner: object,
        owner_token: object,
        fencing_epoch: object,
    ) -> None:
        """Copy the canonical owner's lease onto the gate row; lost ownership raises."""
        registry = self._ownership_registry()
        registry.require(database, owner)
        cursor = database.execute(
            """UPDATE writer_owners
               SET heartbeat_at=(
                       SELECT heartbeat_at FROM maintenance_owners
                       WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                         AND fencing_epoch=?
                   ),
                   expires_at=(
                       SELECT expires_at FROM maintenance_owners
                       WHERE role=? AND scope=? AND actor_id=? AND owner_token=?
                         AND fencing_epoch=?
                   )
               WHERE gate_name='global' AND owner_token=? AND fencing_epoch=?""",
            (
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.token,
                owner.epoch,
                owner.role,
                owner.scope,
                owner.actor_id,
                owner.token,
                owner.epoch,
                owner_token,
                fencing_epoch,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Markdown writer gate ownership was lost before mutation"
            )

    def _assert_writer_ownership(self, database: sqlite3.Connection) -> None:
        owner_token = getattr(self._local, "gate_token", None)
        fencing_epoch = getattr(self._local, "gate_fence", None)
        owner = getattr(self._local, "gate_owner", None)
        if (
            getattr(self, "_database_contract", None) == COORDINATOR_V3_CONTRACT
            and owner is not None
        ):
            self._refresh_v3_writer_lease(database, owner, owner_token, fencing_epoch)
            return
        heartbeat = markdown_store.now()
        cursor = database.execute(
            "UPDATE writer_owners SET heartbeat_at = ?, expires_at = ? "
            "WHERE gate_name = 'global' AND owner_token = ? AND fencing_epoch = ? "
            "AND expires_at > ?",
            (
                heartbeat,
                future_timestamp(markdown_store.WRITER_LEASE_SECONDS),
                owner_token,
                fencing_epoch,
                heartbeat,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Markdown writer gate ownership was lost before mutation")

    def _require_operation_state(
        self, row: sqlite3.Row, expected: str, state_name: str
    ) -> None:
        if self._operation_hash(row) != expected:
            raise TargetStateMismatch(state_name, str(row["path"]))

    def _apply_operation(self, row: sqlite3.Row, operation_plan: Mapping[str, object]) -> None:
        with self._stable_parent(row) as (target, parent_descriptor):
            self._require_operation_target_state(
                target, parent_descriptor, row, "before"
            )
            if parent_descriptor is None:
                self._apply_windows_operation(row, operation_plan, target)
                return
            self._before_target_mutation(target)
            if row["kind"] == "delete":
                self._unlink_at_parent(row, target, parent_descriptor)
                return
            content = self._verified_after_content(row, operation_plan)
            self._publish_at_parent(row, target, parent_descriptor, content)

    @staticmethod
    def _unlink_at_parent(
        row: sqlite3.Row, target: Path, parent_descriptor: int
    ) -> None:
        """A target that vanished under the delete is a before state that moved."""
        try:
            os.unlink(target.name, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise TargetStateMismatch("before", str(row["path"])) from exc
        os.fsync(parent_descriptor)

    def _require_operation_target_state(
        self,
        target: Path,
        parent_descriptor: int | None,
        row: sqlite3.Row,
        state_name: str,
    ) -> None:
        current = self._hash_operation_target(target, parent_descriptor)
        if current != row[f"{state_name}_hash"]:
            raise TargetStateMismatch(state_name, str(row["path"]))

    def _verified_after_content(
        self, row: sqlite3.Row, operation_plan: Mapping[str, object]
    ) -> bytes:
        """The staged after-image, refused unless it hashes and publishes clean."""
        after = operation_plan["after"]
        if not isinstance(after, dict):
            raise RuntimeError("transaction after-image is absent")
        content = self._materialized_state(row, after)
        if sha256_bytes(content) != row["after_hash"]:
            raise TransactionImageError(
                f"transaction after-image is corrupt for {row['path']}"
            )
        self._require_safe_model_output(content, row, operation_plan)
        return content

    def _materialized_state(self, row: sqlite3.Row, state: Mapping[str, object]) -> bytes:
        """The bytes a state stands for, read from its image or rebuilt from the target.

        The target was just checked against this operation's before-hash, so an
        append's after is `current + suffix` and an undone append's is `current[:length]`;
        the caller hashes the result against the row before it is published.
        """
        if "length" in state:
            return self._current_target_bytes(row)[: int(state["length"])]
        if "suffix" in state:
            return self._current_target_bytes(row) + self._transaction_image(row, state["suffix"])
        return self._transaction_image(row, state["artifact"])

    def _transaction_image(self, row: sqlite3.Row, relative: object) -> bytes:
        return image_bytes(self.transaction_root / row["transaction_id"] / str(relative))

    def _current_target_bytes(self, row: sqlite3.Row) -> bytes:
        content = self._read_target(self._target(row["path"]))
        if content is None:
            raise TargetStateMismatch("before", str(row["path"]))
        return content

    def _authenticated_publication_prefix(self, row: sqlite3.Row, plan: Mapping[str, object]) -> bytes | None:
        """The engine's exact before state, never a provider-declared prefix."""
        if row["before_hash"] == ABSENT:
            return None
        before = plan["before"]
        if not isinstance(before, dict):
            return None
        return self._hashed_publication_prefix(row, before)

    def _hashed_publication_prefix(self, row: sqlite3.Row, before: Mapping[str, object]) -> bytes | None:
        content = self._materialized_state(row, before)
        if sha256_bytes(content) != row["before_hash"]:
            return None
        return content

    def _require_safe_model_output(self, content: bytes, row: sqlite3.Row, plan: Mapping[str, object]) -> None:
        """Scan the full after-image with only engine-authenticated provenance."""
        if getattr(self._local, "content_guard", None) != "model_output":
            return
        prefix = self._authenticated_publication_prefix(row, plan)
        if prefix is None:
            require_safe_publication(content)
            return
        require_safe_publication(content, preserved_prefix=prefix)

    def _publish_at_parent(
        self,
        row: sqlite3.Row,
        target: Path,
        parent_descriptor: int,
        content: bytes,
    ) -> None:
        temporary_name = f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            self._write_new_file_at(parent_descriptor, temporary_name, content)
            self._require_operation_target_state(
                target, parent_descriptor, row, "before"
            )
            _link_or_replace(
                row["kind"],
                temporary_name,
                target.name,
                parent_descriptor,
                row["path"],
            )
            os.fsync(parent_descriptor)
            self._require_operation_target_state(
                target, parent_descriptor, row, "after"
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_descriptor)

    def _apply_windows_operation(
        self,
        row: sqlite3.Row,
        operation_plan: Mapping[str, object],
        target: Path,
    ) -> None:
        if getattr(self._local, "windows_parent_handle", None) is None:
            raise RuntimeError("Windows parent handle is not held at mutation")
        if row["kind"] == "delete":
            self._delete_windows_target(target)
            return
        content = self._verified_after_content(row, operation_plan)
        self._publish_windows_target(row, target, content)

    def _delete_windows_target(self, target: Path) -> None:
        target_handle = open_windows_file_for_mutation(target)
        try:
            self._before_target_mutation(target)
            delete_windows_handle(target_handle)
        finally:
            close_windows_handle(target_handle)
        fsync_directory(target.parent)

    def _publish_windows_target(
        self, row: sqlite3.Row, target: Path, content: bytes
    ) -> None:
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            outcome = self._published_windows_outcome(row, target, temporary, content)
        finally:
            _remove_temporary_publication(temporary)
        if outcome == "duplicate":
            fsync_directory(target.parent)

    def _published_windows_outcome(
        self, row: sqlite3.Row, target: Path, temporary: Path, content: bytes
    ) -> str:
        self._write_new_file(temporary, content, owner_only=False)
        self._before_target_mutation(target)
        return durable_publish_file(
            temporary,
            target,
            replace=row["kind"] == "replace",
            expected_sha256=row["after_hash"],
            max_bytes=markdown_store.MAX_KNOWLEDGE_TARGET_BYTES,
        )

    def _before_target_mutation(self, target: Path) -> None:
        """Failure-injection boundary after parent binding and before mutation."""
        del target
        self._require_current_operation_active()

    def _require_current_operation_active(self) -> None:
        deadline = getattr(self._local, "recovery_deadline", None)
        cancelled = getattr(self._local, "recovery_cancelled", None)
        if deadline is not None and self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("transaction mutation deadline or cancellation reached")

    def _require_operation_active(
        self, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        if self._recovery_stopped(deadline, cancelled):
            raise TimeoutError("transaction mutation deadline or cancellation reached")

    def _write_new_file_at(self, parent_descriptor: int, name: str, content: bytes) -> None:
        descriptor = self._create_at_parent(parent_descriptor, name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _create_at_parent(parent_descriptor: int, name: str) -> int:
        """`O_CREAT` cannot miss a file, so an ENOENT here is the parent, gone."""
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_descriptor)
        except FileNotFoundError as exc:
            raise TargetBoundaryFailure(
                "parent identity is not a stable directory while writing"
            ) from exc
