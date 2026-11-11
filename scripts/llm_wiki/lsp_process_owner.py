"""The owner root of one LSP child, its directory, identities and published records."""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import stat
import subprocess as _subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import windows_workspace as _windows_workspace
from compile_cache import acl_principal  # noqa: E402
from llm_wiki.lsp_process_limits import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    MAX_ACL_OUTPUT_BYTES,
    MAX_EVIDENCE_BYTES,
    MAX_PENDING_CHILD_HANDLES,
    MAX_PENDING_TEMP_NAMES,
    WINDOWS_LEASE_RETRY_ERRORS,
    WINDOWS_LEASE_RETRY_SECONDS,
)
from llm_wiki.windows_acl import acl_output_text, windows_acl_identity  # noqa: E402


@dataclass(frozen=True, slots=True)
class ObjectIdentity:
    device: int
    inode: int
    file_type: int
    reparse_attributes: int


def _read_bounded_descriptor(descriptor: int) -> bytes:
    """Read the whole file, refusing anything past the evidence bound."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(4096, MAX_EVIDENCE_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_EVIDENCE_BYTES:
            raise ValueError("LSP evidence record exceeds its byte bound")


def canonical_evidence_record(payload: bytes) -> dict[str, object]:
    """The record these bytes hold, if they are its own canonical encoding."""
    try:
        record = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("LSP evidence record is not canonical JSON") from exc
    if not isinstance(record, dict):
        raise ValueError("LSP evidence record is not a JSON object")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if canonical != payload:
        raise ValueError("LSP evidence record is not canonical JSON")
    return record


def _evidence_payload(record: Mapping[str, object]) -> bytes:
    """One evidence record as compact JSON bytes, within its bound."""
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise ValueError("LSP evidence record exceeds its byte bound")
    return payload


def _first_error(
    errors: Sequence[BaseException | None],
) -> BaseException | None:
    """The first error among the results, if any of them failed."""
    for error in errors:
        if error is not None:
            return error
    return None


def _verify_cancellation_identity_stable(cancellation: int) -> None:
    identity = _windows_workspace.identity(cancellation, directory=True)
    if _windows_workspace.identity(cancellation, directory=True) != identity:
        raise PermissionError(
            "LSP cancellation root identity changed during ACL setup"
        )


def _flush_owner_directory_windows(handle: int) -> None:
    if not _windows_workspace.flush_directory(handle):
        raise OSError("LSP owner directory durability flush failed")


def _lease_payload(record: Mapping[str, object]) -> bytes:
    """The lease as compact JSON bytes, within its evidence bound."""
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise ValueError("LSP lease exceeds its byte bound")
    return payload


def _open_owner_parent_posix(parent: Path) -> tuple[int, object]:
    """A no-follow descriptor on the owner's parent, and its identity."""
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError(
            "LSP ownership requires POSIX no-follow directory APIs"
        )
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
        )
    except NotADirectoryError as exc:
        raise ValueError("owner_root parent must be a no-follow directory") from exc
    try:
        identity = _identity_from_stat(os.fstat(descriptor))
        _require_directory_identity(identity, "owner_root parent")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, identity


def _open_owner_parent_windows(parent: Path) -> tuple[int, object]:
    """A handle on the owner's parent, and its identity."""
    handle = _windows_workspace.open_directory_path(parent)
    try:
        identity = _windows_workspace.identity(handle, directory=True)
    except BaseException:
        _windows_workspace.close_handle(handle)
        raise
    return handle, identity


@dataclass(slots=True)
class OwnerDirectory:
    owner_root: Path
    parent_handle: int
    parent_identity: object
    owner_handle: int | None = None
    owner_identity: object | None = None
    owner_permissions_verified: bool = False
    _close_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _child_handle_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    _pending_child_handles: list[int] = field(default_factory=list, repr=False)
    _pending_temp_names: list[str] = field(default_factory=list, repr=False)
    _lease_expires_monotonic: float | None = field(default=None, repr=False)
    _closed: bool = False

    def _remember_temp_name(self, name: str) -> None:
        if name in self._pending_temp_names:
            return
        if len(self._pending_temp_names) >= MAX_PENDING_TEMP_NAMES:
            raise RuntimeError("LSP pending temporary name bound was exhausted")
        self._pending_temp_names.append(name)

    def _forget_temp_name(self, name: str) -> None:
        if name in self._pending_temp_names:
            self._pending_temp_names.remove(name)

    def _retry_pending_child_handles(self) -> None:
        """Try every pending handle; raise the first failure after all were tried."""
        attempts = [self._close_pending_child(handle) for handle in tuple(self._pending_child_handles)]
        errors = [error for error in attempts if error is not None]
        if errors:
            raise errors[0]

    def _close_pending_child(self, handle: int) -> BaseException | None:
        try:
            _windows_workspace.close_handle(handle)
        except BaseException as error:
            return error
        self._pending_child_handles.remove(handle)
        return None

    def _retry_posix_temp_name(self, name: str) -> None:
        """Remove one leftover; an absent file is already the outcome we want."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=self.owner_handle)
        self._forget_temp_name(name)

    def _delete_temp_handle(self, handle: object) -> None:
        """Delete through the handle and always close it, keeping the first error."""
        delete_error: BaseException | None = None
        try:
            _windows_workspace.delete_handle(handle)
        except BaseException as error:
            delete_error = error
        try:
            self._close_child_handle(handle)
        except BaseException as close_error:
            if delete_error is not None:
                raise delete_error from close_error
            raise
        if delete_error is not None:
            raise delete_error

    def _retry_windows_temp_name(self, name: str) -> None:
        try:
            handle = _windows_workspace.open_deletable_file(self.owner_handle, name)
        except FileNotFoundError:
            self._forget_temp_name(name)
            return
        self._delete_temp_handle(handle)
        self._forget_temp_name(name)

    def _retry_one_temp_name(self, name: str) -> None:
        if os.name == "posix":
            self._retry_posix_temp_name(name)
            return
        if os.name != "nt":
            raise RuntimeError(
                "LSP temporary cleanup is unsupported on this platform"
            )
        self._retry_windows_temp_name(name)

    def _retry_pending_temp_names(self) -> None:
        with self._child_handle_lock:
            if os.name == "nt":
                self._retry_pending_child_handles()
            if self._pending_temp_names and self.owner_handle is None:
                raise RuntimeError("LSP pending temporary owner is closed")
            for name in tuple(self._pending_temp_names):
                self._retry_one_temp_name(name)

    def _mark_temporary_deleted(
        self, handle: int, *, moved: bool, operation_error: BaseException | None
    ) -> tuple[bool, BaseException | None]:
        """Mark the leftover for deletion; (marked, why it could not be)."""
        if operation_error is None or moved:
            return False, None
        try:
            _windows_workspace.delete_handle(handle)
        except BaseException as error:
            return False, error
        return True, None

    def _close_temporary_handle(
        self,
        handle: int,
        cleanup_error: BaseException | None,
        operation_error: BaseException | None,
    ) -> None:
        """Close the handle, chaining whatever already went wrong behind it."""
        try:
            self._close_child_handle(handle)
        except BaseException as close_error:
            if cleanup_error is not None:
                raise close_error from cleanup_error
            if operation_error is not None:
                raise close_error from operation_error
            raise

    def _finish_windows_temporary(
        self,
        name: str,
        handle: int,
        *,
        moved: bool,
        operation_error: BaseException | None,
    ) -> None:
        delete_marked, cleanup_error = self._mark_temporary_deleted(
            handle, moved=moved, operation_error=operation_error
        )
        if moved:
            self._forget_temp_name(name)
        self._close_temporary_handle(handle, cleanup_error, operation_error)
        if delete_marked:
            self._forget_temp_name(name)
        self._raise_temporary_errors(cleanup_error, operation_error)

    @staticmethod
    def _raise_temporary_errors(
        cleanup_error: BaseException | None, operation_error: BaseException | None
    ) -> None:
        if cleanup_error is not None:
            raise cleanup_error from operation_error
        if operation_error is not None:
            raise operation_error

    def _close_child_handle(self, handle: int) -> None:
        try:
            _windows_workspace.close_handle(handle)
        except BaseException:
            self._remember_pending_child_handle(handle)
            raise

    def _remember_pending_child_handle(self, handle: int) -> None:
        if handle in self._pending_child_handles:
            return
        if len(self._pending_child_handles) >= MAX_PENDING_CHILD_HANDLES:
            raise RuntimeError("LSP pending child handle bound was exhausted")
        self._pending_child_handles.append(handle)

    def _close_one_child_handle(self, handle: int | None) -> BaseException | None:
        """Close one handle; the error it raised, if it raised one."""
        if handle is None:
            return None
        try:
            self._close_child_handle(handle)
        except BaseException as error:
            return error
        return None

    def _close_child_handles(self, *handles: int | None) -> None:
        errors = [self._close_one_child_handle(handle) for handle in handles]
        first_error = _first_error(errors)
        if first_error is not None:
            raise first_error

    @classmethod
    def open(cls, owner_root: Path) -> OwnerDirectory:
        if os.name == "posix":
            descriptor, identity = _open_owner_parent_posix(owner_root.parent)
            return cls(owner_root, descriptor, identity)
        if os.name == "nt":
            handle, identity = _open_owner_parent_windows(owner_root.parent)
            return cls(owner_root, handle, identity)
        raise RuntimeError(
            "LSP owner directories are unsupported on this platform"
        )

    def _create_owner_root_posix(self) -> int:
        """Create and verify the owner root, returning its descriptor."""
        os.mkdir(self.owner_root.name, 0o700, dir_fd=self.parent_handle)
        owner = os.open(
            self.owner_root.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=self.parent_handle,
        )
        self.owner_handle = owner
        self.owner_identity = _identity_from_stat(os.fstat(owner))
        _require_directory_identity(self.owner_identity, "LSP owner root")
        os.fchmod(owner, 0o700)
        _verify_descriptor(owner, self.owner_identity, mode=0o700, directory=True)
        self.owner_permissions_verified = True
        return owner

    @staticmethod
    def _create_cancellation_posix(owner: int) -> None:
        """Create and verify the cancellation directory inside the owner root."""
        os.mkdir("cancellation", 0o700, dir_fd=owner)
        cancellation = os.open(
            "cancellation",
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=owner,
        )
        try:
            identity = _identity_from_stat(os.fstat(cancellation))
            _require_directory_identity(identity, "LSP cancellation root")
            os.fchmod(cancellation, 0o700)
            _verify_descriptor(
                cancellation, identity, mode=0o700, directory=True
            )
            os.fsync(cancellation)
        finally:
            os.close(cancellation)

    def _create_posix(self) -> None:
        owner = self._create_owner_root_posix()
        self._create_cancellation_posix(owner)
        os.fsync(owner)
        os.fsync(self.parent_handle)

    def _create_owner_root_windows(self, deadline: float) -> int:
        """Create the owner root and prove its ACL setup did not move it."""
        owner = _windows_workspace.create_writable_directory(
            self.parent_handle, self.owner_root.name
        )
        self.owner_handle = owner
        self.owner_identity = _windows_workspace.identity(owner, directory=True)
        secure_windows_owner_root(self.owner_root, deadline)
        if _windows_workspace.identity(owner, directory=True) != self.owner_identity:
            raise PermissionError(
                "LSP owner root identity changed during ACL setup"
            )
        self.owner_permissions_verified = True
        return owner

    def _create_cancellation_windows(self, owner: int) -> None:
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            cancellation = _windows_workspace.create_directory(
                owner, "cancellation"
            )
            try:
                _verify_cancellation_identity_stable(cancellation)
            finally:
                self._close_child_handle(cancellation)

    def _create_windows(self, deadline: float) -> None:
        owner = self._create_owner_root_windows(deadline)
        self._create_cancellation_windows(owner)

    def create(self, deadline: float) -> None:
        if os.name == "posix":
            self._create_posix()
            return
        self._create_windows(deadline)

    def _publish_record_posix(
        self, temporary: str, name: str, payload: bytes
    ) -> None:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=self.owner_handle,
        )
        self._remember_temp_name(temporary)
        try:
            self._write_record_descriptor(descriptor, payload)
        finally:
            os.close(descriptor)
        os.link(
            temporary,
            name,
            src_dir_fd=self.owner_handle,
            dst_dir_fd=self.owner_handle,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=self.owner_handle)
        self._forget_temp_name(temporary)
        self.sync_directory()

    def _write_record_descriptor(self, descriptor: int, payload: bytes) -> None:
        """Write and verify one evidence record through an owned descriptor."""
        identity = _identity_from_stat(os.fstat(descriptor))
        _require_file_identity(identity, "LSP evidence record")
        write_all_descriptor(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _verify_descriptor(descriptor, identity, mode=0o600, directory=False)

    def _write_record_posix(self, temporary: str, name: str, payload: bytes) -> None:
        try:
            self._publish_record_posix(temporary, name, payload)
        except BaseException as operation_error:
            self._retry_temp_names_after(temporary, operation_error)
            raise

    def _publish_record_windows(
        self, handle: object, name: str, payload: bytes
    ) -> None:
        """Write the record and publish it only if its identity never moved."""
        identity = _windows_workspace.identity(handle, directory=False)
        _windows_workspace.write_all(
            handle, payload, chunk_bytes=MAX_EVIDENCE_BYTES
        )
        _windows_workspace.flush_file(handle)
        if _windows_workspace.identity(handle, directory=False) != identity:
            raise PermissionError("LSP evidence identity changed during write")
        _windows_workspace.publish_file(handle, self.owner_handle, name)

    def _write_record_windows(
        self, temporary: str, name: str, payload: bytes
    ) -> None:
        if not self.owner_permissions_verified:
            raise PermissionError(
                "LSP owner ACL was not verified before evidence creation"
            )
        self._remember_temp_name(temporary)
        try:
            handle = _windows_workspace.create_file(self.owner_handle, temporary)
        except BaseException as operation_error:
            self._retry_temp_names_always(operation_error)
            raise
        published = False
        operation_error: BaseException | None = None
        try:
            self._publish_record_windows(handle, name, payload)
            published = True
        except BaseException as error:
            operation_error = error
        self._finish_windows_temporary(
            temporary,
            handle,
            moved=published,
            operation_error=operation_error,
        )
        if not published:
            raise OSError("LSP evidence publication did not complete")
        # The handle is let go before the directory is flushed, as the lease path
        # already does. Publication is a rename through a handle that shares
        # nothing, so until it closes the record stands under its contract name
        # and no reader can open it - for as long as a directory FlushFileBuffers
        # takes on that disk. Research:
        # docs/research/2026-09-18-a-published-record-is-let-go-before-it-is-flushed.md
        self.sync_directory()

    def write_record(
        self,
        name: str,
        record: Mapping[str, object],
    ) -> None:
        if name not in {"owner.json", "failure.json"} or self.owner_handle is None:
            raise ValueError("LSP evidence name or owner handle is invalid")
        payload = _evidence_payload(record)
        with self._child_handle_lock:
            self._retry_pending_temp_names()
            temporary = f".evidence-{secrets.token_hex(8)}.tmp"
            if os.name == "posix":
                self._write_record_posix(temporary, name, payload)
                return
            self._write_record_windows(temporary, name, payload)

    def _lease_retry_deadline(self, deadline: float | None) -> float:
        """How long a replace may keep retrying, never past our own lease."""
        retry_deadline = (
            time.monotonic() if deadline is None else validated_deadline(deadline)
        )
        if self._lease_expires_monotonic is None:
            return retry_deadline
        return min(retry_deadline, self._lease_expires_monotonic)

    def _retry_temp_names_always(self, operation_error: BaseException) -> None:
        """Clean up leftovers; a cleanup failure outranks the original error."""
        try:
            self._retry_pending_temp_names()
        except BaseException as cleanup_error:
            raise cleanup_error from operation_error

    def _retry_temp_names_after(
        self, temporary: str, operation_error: BaseException
    ) -> None:
        if temporary not in self._pending_temp_names:
            return
        self._retry_temp_names_always(operation_error)

    def _write_lease_descriptor(self, descriptor: int, payload: bytes) -> None:
        """Write and verify the lease through an owned descriptor."""
        identity = _identity_from_stat(os.fstat(descriptor))
        _require_file_identity(identity, "LSP lease")
        write_all_descriptor(descriptor, payload)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        _verify_descriptor(descriptor, identity, mode=0o600, directory=False)

    def _publish_lease_posix(
        self, temporary: str, payload: bytes, expires_monotonic: float | None
    ) -> None:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=self.owner_handle,
        )
        self._remember_temp_name(temporary)
        try:
            self._write_lease_descriptor(descriptor, payload)
        finally:
            os.close(descriptor)
        os.replace(
            temporary,
            "lease.json",
            src_dir_fd=self.owner_handle,
            dst_dir_fd=self.owner_handle,
        )
        self._forget_temp_name(temporary)
        self.sync_directory()
        self._lease_expires_monotonic = expires_monotonic

    def _write_lease_posix(
        self, temporary: str, payload: bytes, expires_monotonic: float | None
    ) -> None:
        try:
            self._publish_lease_posix(temporary, payload, expires_monotonic)
        except BaseException as operation_error:
            self._retry_temp_names_after(temporary, operation_error)
            raise

    @staticmethod
    def _lease_replace_retryable(
        error: OSError, retry_deadline: float, retry_stop: threading.Event | None
    ) -> bool:
        """Whether this replace failure is one we wait out rather than report."""
        code = getattr(error, "winerror", None) or error.errno
        if code not in WINDOWS_LEASE_RETRY_ERRORS or retry_stop is None:
            return False
        remaining = retry_deadline - time.monotonic()
        if remaining <= 0:
            return False
        return not retry_stop.wait(min(WINDOWS_LEASE_RETRY_SECONDS, remaining))

    def _replace_lease_windows(
        self,
        handle: object,
        retry_deadline: float,
        retry_stop: threading.Event | None,
    ) -> None:
        while True:
            if self._lease_replaced_windows(handle, retry_deadline, retry_stop):
                return

    def _lease_replaced_windows(
        self,
        handle: object,
        retry_deadline: float,
        retry_stop: threading.Event | None,
    ) -> bool:
        try:
            _windows_workspace.replace_file(handle, self.owner_handle, "lease.json")
        except OSError as error:
            if not self._lease_replace_retryable(error, retry_deadline, retry_stop):
                raise
            return False
        return True

    def _write_lease_windows(
        self,
        temporary: str,
        payload: bytes,
        expires_monotonic: float | None,
        retry_deadline: float,
        retry_stop: threading.Event | None,
    ) -> None:
        self._remember_temp_name(temporary)
        try:
            handle = _windows_workspace.create_file(self.owner_handle, temporary)
        except BaseException as operation_error:
            self._retry_temp_names_always(operation_error)
            raise
        replaced = False
        operation_error: BaseException | None = None
        try:
            _windows_workspace.write_all(
                handle, payload, chunk_bytes=MAX_EVIDENCE_BYTES
            )
            _windows_workspace.flush_file(handle)
            self._replace_lease_windows(handle, retry_deadline, retry_stop)
            replaced = True
            self._lease_expires_monotonic = expires_monotonic
        except BaseException as error:
            operation_error = error
        self._finish_windows_temporary(
            temporary,
            handle,
            moved=replaced,
            operation_error=operation_error,
        )
        self.sync_directory()

    def write_lease(
        self,
        record: Mapping[str, object],
        *,
        deadline: float | None = None,
        expires_monotonic: float | None = None,
        retry_stop: threading.Event | None = None,
    ) -> None:
        if self.owner_handle is None:
            raise RuntimeError("LSP owner directory is closed")
        retry_deadline = self._lease_retry_deadline(deadline)
        if expires_monotonic is not None:
            expires_monotonic = validated_deadline(expires_monotonic)
        payload = _lease_payload(record)
        with self._child_handle_lock:
            self._write_lease_locked(payload, expires_monotonic, retry_deadline, retry_stop)

    def _write_lease_locked(
        self,
        payload: bytes,
        expires_monotonic: float | None,
        retry_deadline: float,
        retry_stop: threading.Event | None,
    ) -> None:
        self._retry_pending_temp_names()
        temporary = f".lease-{secrets.token_hex(8)}.tmp"
        if os.name == "posix":
            self._write_lease_posix(temporary, payload, expires_monotonic)
            return
        self._write_lease_windows(
            temporary,
            payload,
            expires_monotonic,
            retry_deadline,
            retry_stop,
        )

    def sync_directory(self) -> None:
        handle = self.owner_handle
        if handle is None:
            raise RuntimeError("LSP owner directory is closed")
        self._sync_directory_handle(handle)

    @staticmethod
    def _sync_directory_handle(handle: int) -> None:
        if os.name == "posix":
            os.fsync(handle)
            return
        if os.name == "nt":
            _flush_owner_directory_windows(handle)
            return
        raise RuntimeError("LSP owner directories are unsupported on this platform")

    def _remove_lease_posix(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink("lease.json", dir_fd=self.owner_handle)
            os.fsync(self.owner_handle)

    def _remove_lease_windows(self) -> None:
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            try:
                handle = _windows_workspace.open_deletable_file(
                    self.owner_handle, "lease.json"
                )
            except FileNotFoundError:
                return
            self._delete_temp_handle(handle)

    def remove_lease(self) -> None:
        if self.owner_handle is None:
            self._require_no_pending_temp_names()
            return
        self._retry_pending_temp_names()
        if os.name == "posix":
            self._remove_lease_posix()
        else:
            self._remove_lease_windows()
        self._lease_expires_monotonic = None

    def _require_no_pending_temp_names(self) -> None:
        if self._pending_temp_names:
            raise RuntimeError("LSP pending temporary owner is closed")

    def _read_record_posix(self, name: str) -> bytes:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=self.owner_handle,
        )
        try:
            identity = _identity_from_stat(os.fstat(descriptor))
            _require_file_identity(identity, "LSP evidence record")
            return _read_bounded_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _read_record_windows(self, name: str) -> bytes:
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            handle = _windows_workspace.open_file(self.owner_handle, name)
            try:
                return b"".join(
                    _windows_workspace.read_chunks(
                        handle,
                        chunk_bytes=4096,
                        max_bytes=MAX_EVIDENCE_BYTES,
                    )
                )
            finally:
                self._close_child_handle(handle)

    def read_record(self, name: str) -> dict[str, object]:
        if name not in {"owner.json", "failure.json"} or self.owner_handle is None:
            raise ValueError("LSP evidence name or owner handle is invalid")
        if os.name == "posix":
            payload = self._read_record_posix(name)
        else:
            payload = self._read_record_windows(name)
        return canonical_evidence_record(payload)

    def _unlink_posix_scratch(self) -> None:
        """Remove the evidence files, native launch copies and the cancellation directory.

        A native server's verified copy (`.launch-*`) stayed behind, the root's
        `rmdir` failed on every close, and the session was stranded (audit
        2026-09-26 A-6, docs/research/2026-09-26-a-native-server-copy-leaves-with-its-owner.md).
        """
        for name in ("owner.json", "failure.json", *_launch_copies(self.owner_handle)):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self.owner_handle)
        with contextlib.suppress(FileNotFoundError):
            os.rmdir("cancellation", dir_fd=self.owner_handle)
        os.fsync(self.owner_handle)

    def _named_owner_identity(self) -> object | None:
        """The identity of our owner root as its parent names it, if it is there."""
        try:
            return _identity_from_stat(
                os.stat(
                    self.owner_root.name,
                    dir_fd=self.parent_handle,
                    follow_symlinks=False,
                )
            )
        except FileNotFoundError:
            return None

    def _remove_posix_owner_root(self) -> None:
        """Remove our owner root, and only ours."""
        named_identity = self._named_owner_identity()
        if named_identity is None:
            return
        if named_identity != self.owner_identity:
            raise PermissionError("LSP owner root identity changed before deletion")
        os.rmdir(self.owner_root.name, dir_fd=self.parent_handle)

    def _remove_success_scratch_posix(self) -> None:
        if self.owner_handle is None:
            return
        self._unlink_posix_scratch()
        self._remove_posix_owner_root()
        os.fsync(self.parent_handle)

    def _delete_child(self, handle: object) -> None:
        try:
            _windows_workspace.delete_handle(handle)
        finally:
            self._close_child_handle(handle)

    def _delete_windows_scratch_files(self, original: object) -> None:
        for name in ("owner.json", "failure.json"):
            try:
                handle = _windows_workspace.open_deletable_file(original, name)
            except FileNotFoundError:
                continue
            self._delete_child(handle)

    def _delete_windows_cancellation(self, original: object) -> None:
        try:
            cancellation = _windows_workspace.open_deletable_directory(
                original, "cancellation"
            )
        except FileNotFoundError:
            return
        self._delete_child(cancellation)

    def _close_windows_scratch(self) -> None:
        """Delete everything inside the owner root, then let the handle go."""
        original = self.owner_handle
        if original is None:
            return
        self._delete_windows_scratch_files(original)
        self._delete_windows_cancellation(original)
        _windows_workspace.close_handle(original)
        self.owner_handle = None

    def _remove_windows_owner_root(self, expected: object) -> None:
        """Remove our owner root, and only ours."""
        try:
            owner = _windows_workspace.open_deletable_directory(
                self.parent_handle, self.owner_root.name
            )
        except FileNotFoundError:
            return
        try:
            if _windows_workspace.identity(owner, directory=True) != expected:
                raise PermissionError(
                    "LSP owner root identity changed before deletion"
                )
            _windows_workspace.delete_handle(owner)
        finally:
            self._close_child_handle(owner)

    def _remove_success_scratch_windows(self) -> None:
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            expected = self.owner_identity
            self._close_windows_scratch()
            self._remove_windows_owner_root(expected)

    def remove_success_scratch(self) -> None:
        self._retry_pending_temp_names()
        if os.name == "posix":
            self._remove_success_scratch_posix()
            return
        self._remove_success_scratch_windows()

    def _verify_lexical_identity_posix(self) -> None:
        if _current_identity(self.owner_root.parent) != self.parent_identity:
            raise RuntimeError("owner_root parent identity changed during startup")
        if _current_identity(self.owner_root) != self.owner_identity:
            raise RuntimeError("LSP owner root identity changed during startup")
        _verify_descriptor(
            self.owner_handle, self.owner_identity, mode=0o700, directory=True
        )

    def _check_windows_parent_identity(self, parent: int) -> None:
        if (
            _windows_workspace.identity(parent, directory=True)
            != self.parent_identity
        ):
            raise RuntimeError("owner_root parent identity changed during startup")

    def _check_windows_named_identity(self, named: int) -> None:
        if _windows_workspace.identity(named, directory=True) != self.owner_identity:
            raise RuntimeError("LSP owner root identity changed during startup")

    def _check_windows_held_identity(self) -> None:
        if (
            _windows_workspace.identity(self.owner_handle, directory=True)
            != self.owner_identity
        ):
            raise RuntimeError(
                "held LSP owner root identity changed during startup"
            )

    def _verify_lexical_identity_windows(self) -> None:
        with self._child_handle_lock:
            self._retry_pending_child_handles()
            parent = _windows_workspace.open_directory_path(self.owner_root.parent)
            named: int | None = None
            try:
                self._check_windows_parent_identity(parent)
                named = _windows_workspace.open_directory(
                    parent, self.owner_root.name
                )
                self._check_windows_named_identity(named)
                self._check_windows_held_identity()
            finally:
                self._close_child_handles(named, parent)

    def verify_lexical_identity(self) -> None:
        if self.owner_handle is None or self.owner_identity is None:
            raise RuntimeError("LSP owner directory was not created")
        if os.name == "posix":
            self._verify_lexical_identity_posix()
            return
        self._verify_lexical_identity_windows()

    def _close_native_handle(self, handle: int) -> None:
        """Close one handle the way this platform closes handles."""
        if os.name == "posix":
            os.close(handle)
            return
        if os.name == "nt":
            _windows_workspace.close_handle(handle)

    def _forget_closed_handle(self, label: str, handle: int) -> None:
        with self._close_lock:
            if label == "owner" and self.owner_handle == handle:
                self.owner_handle = None
                return
            if label == "parent" and self.parent_handle == handle:
                self.parent_handle = -1

    def _release_handle(self, label: str, handle: int | None) -> BaseException | None:
        """Close and forget one handle; the error it raised, if it raised one."""
        if handle is None or handle < 0:
            return None
        try:
            self._close_native_handle(handle)
        except BaseException as exc:
            return exc
        self._forget_closed_handle(label, handle)
        return None

    def _claim_close(self) -> tuple[int | None, int] | None:
        """The handles to close, or None when this directory is already closed."""
        with self._close_lock:
            if self._closed:
                return None
            return self.owner_handle, self.parent_handle

    def close(self) -> None:
        with self._child_handle_lock:
            self._retry_pending_temp_names()
            claimed = self._claim_close()
            if claimed is None:
                return
            owner, parent = claimed
            errors = [
                self._release_handle(label, handle)
                for label, handle in (("owner", owner), ("parent", parent))
            ]
            with self._close_lock:
                self._closed = self.owner_handle is None and self.parent_handle < 0
            first_error = _first_error(errors)
            if first_error is not None:
                raise first_error


def _launch_copies(directory_handle: int) -> list[str]:
    """The regular `.launch-*` files directly inside an owner root."""
    return [name for name in os.listdir(directory_handle) if _is_launch_copy(directory_handle, name)]


def _is_launch_copy(directory_handle: int, name: str) -> bool:
    if not name.startswith(".launch-"):
        return False
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=directory_handle, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _object_identity(path: Path) -> ObjectIdentity:
    return _identity_from_stat(path.lstat())


def _identity_from_stat(info: os.stat_result) -> ObjectIdentity:
    return ObjectIdentity(
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(getattr(info, "st_file_attributes", 0)) & FILE_ATTRIBUTE_REPARSE_POINT,
    )


def _require_directory_identity(identity: ObjectIdentity, label: str) -> None:
    if identity.file_type == stat.S_IFLNK or identity.reparse_attributes:
        raise ValueError(f"{label} must not be a symlink or reparse point")
    if identity.file_type != stat.S_IFDIR:
        raise ValueError(f"{label} must be a directory")


def _require_file_identity(identity: ObjectIdentity, label: str) -> None:
    if identity.file_type != stat.S_IFREG or identity.reparse_attributes:
        raise PermissionError(f"{label} has an unsafe identity")


def _verify_descriptor(
    descriptor: int,
    expected: ObjectIdentity,
    *,
    mode: int,
    directory: bool,
) -> None:
    current = _identity_from_stat(os.fstat(descriptor))
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if current != expected or current.file_type != expected_type:
        raise PermissionError("held LSP artifact identity changed")
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        raise PermissionError("held LSP artifact is not owner-only")


def _current_identity(path: Path) -> ObjectIdentity | None:
    try:
        return _object_identity(path)
    except (FileNotFoundError, OSError):
        return None


# Explicit access control entries survive `/inheritance:r`, which only drops
# inherited ones. Windows images commonly place explicit entries for SYSTEM,
# Administrators and OWNER RIGHTS on the temporary tree, so the owner-only
# guarantee needs the same removal passes `_harden_cache_windows_acl` already
# uses for the cache root.
BROAD_ACL_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-3-0",  # Creator Owner
    "*S-1-3-4",  # Owner Rights
    "*S-1-5-18",  # Local System
    "*S-1-5-32-544",  # Administrators
    "*S-1-5-32-545",  # Users
    "*S-1-15-2-1",  # All application packages
    "*S-1-15-2-2",  # All restricted application packages
)


def _owner_only_acl_commands(path: Path, identity: str) -> tuple[list[str], ...]:
    target = str(path)
    return (
        ["icacls", target, "/inheritance:r", "/grant:r", f"{identity}:(OI)(CI)(F)"],
        ["icacls", target, "/remove:g", *BROAD_ACL_SIDS],
        ["icacls", target, "/remove:d", *BROAD_ACL_SIDS],
    )


def _apply_owner_only_acl(path: Path, identity: str, deadline: float) -> None:
    for command in _owner_only_acl_commands(path, identity):
        applied = _run_windows_owner_acl(command, deadline)
        if applied.returncode != 0:
            raise PermissionError("could not apply owner-only LSP ACL")


def _read_owner_acl_entries(path: Path, deadline: float) -> list[str]:
    verified = _run_windows_owner_acl(["icacls", str(path)], deadline)
    if verified.returncode != 0:
        raise PermissionError("could not verify owner-only LSP ACL")
    return [
        line.strip()
        for line in acl_output_text(verified.stdout).splitlines()
        if ":(" in line
    ]


def require_single_owner_ace(path: Path, entries: Sequence[str]) -> str:
    if len(entries) == 1:
        return entries[0]
    # Name the principals that were actually present. Without them an operator
    # cannot tell an extra inherited entry from a missing one, and the platform
    # that produced the entries is usually not the one reading the failure.
    principals = ", ".join(sorted({acl_principal(path, entry) for entry in entries})[:8])
    raise PermissionError(
        "owner-only LSP ACL verification was ambiguous: expected 1 access control "
        f"entry, found {len(entries)} for {principals}"
    )


def _require_owner_only_ace(path: Path, owner_ace: str, identity: str) -> None:
    markers = re.findall(r"\([^)]+\)", owner_ace)
    if (
        acl_principal(path, owner_ace).casefold() != identity.casefold()
        or len(markers) != 3
        or set(markers) != {"(OI)", "(CI)", "(F)"}
    ):
        raise PermissionError("owner-only LSP ACL verification failed")


def secure_windows_owner_root(path: Path, deadline: float) -> None:
    identity = windows_acl_identity()
    _apply_owner_only_acl(path, identity, deadline)
    entries = _read_owner_acl_entries(path, deadline)
    owner_ace = require_single_owner_ace(path, entries)
    _require_owner_only_ace(path, owner_ace, identity)


def _run_windows_owner_acl(
    command: Sequence[str], deadline: float
) -> _subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PermissionError("owner-only LSP ACL deadline expired")
    try:
        result = _subprocess.run(
            list(command),
            shell=False,
            check=False,
            capture_output=True,
            timeout=remaining,
        )
    except (OSError, _subprocess.TimeoutExpired) as exc:
        raise PermissionError("owner-only LSP ACL command failed") from exc
    _check_acl_output_bound(result)
    return result


def _check_acl_output_bound(
    result: _subprocess.CompletedProcess[bytes],
) -> None:
    """An ACL tool may not hand us more output than we agreed to read."""
    produced = len(result.stdout or b"") + len(result.stderr or b"")
    if produced > MAX_ACL_OUTPUT_BYTES:
        raise PermissionError("owner-only LSP ACL output exceeded its byte bound")


def write_all_descriptor(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("LSP evidence write made no progress")
        offset += written


def new_generation_nonce() -> str:
    nonce = secrets.token_hex(16)
    if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("generation nonce must be 32 lowercase hexadecimal characters")
    return nonce


def validated_deadline(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)
