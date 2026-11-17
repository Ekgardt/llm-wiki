"""Close a session: reserve the close, shut down every owned target, forget the session state."""

import time
from dataclasses import dataclass

from interruption import raise_collected_errors as _raise_collected_errors
from llm_wiki.pyright_session_values import LOCK_POLL_SECONDS, validated_deadline
from lsp_process import LspProcess, StartupCleanupError


@dataclass(frozen=True)
class _CloseTargets:
    """What a close has to shut down, captured under the state lock."""

    cleanup_error: StartupCleanupError | None
    startup_process: LspProcess | None
    process: LspProcess | None


def _sleep_before_close_retry(deadline: float) -> None:
    """Back off once while an operation finishes, or give up on the deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Pyright operations did not finish before close")
    time.sleep(min(LOCK_POLL_SECONDS, remaining))


def _close_owned_targets(targets: _CloseTargets, deadline: float) -> None:
    """Close what the session owns, in the order it took them on."""
    if targets.cleanup_error is not None:
        targets.cleanup_error.retry_cleanup(deadline)
    _close_owned_processes(targets, deadline)


def _close_owned_processes(targets: _CloseTargets, deadline: float) -> None:
    for process in (targets.startup_process, targets.process):
        if process is not None:
            process.close(deadline)


class SessionClose:
    """Close the session and every process and owner it holds."""

    def close(self, *, deadline: float) -> None:
        """Close what this session owns; a failure leaves it stranded, not lost.

        A close that raises after `_closing` was set leaves a session nobody is
        closing. It is marked, so the manager's next caller retries the close
        under its own deadline instead of polling a flag that never clears.
        """
        deadline = validated_deadline(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._close_lock.acquire(timeout=remaining):
            raise TimeoutError("Pyright close serialization deadline expired")
        try:
            self._close_attempt_recorded(deadline)
        finally:
            self._close_lock.release()

    def _close_attempt_recorded(self, deadline: float) -> None:
        """Close under the close lock, remembering one that did not finish.

        `_close_stranded` is written only by the thread holding `_close_lock`,
        so the flag is never set while another thread is still closing: it
        means "the last close attempt ended badly and nobody took it up".
        """
        self._close_stranded = False
        try:
            self._close_owned(deadline)
        except BaseException:
            self._close_stranded = True
            raise

    def _close_finished_locked(
        self,
        cleanup_error: StartupCleanupError | None,
        startup_process: LspProcess | None,
    ) -> bool:
        """Nothing of ours is left to close."""
        return (
            self._closed
            and self._process is None
            and cleanup_error is None
            and startup_process is None
        )

    def _close_attempt_locked(self) -> tuple[bool, _CloseTargets | None]:
        """Whether the close already happened, and what is ready to be closed."""
        cleanup_error = self._startup_cleanup_error
        startup_process = self._startup_process
        if self._close_finished_locked(cleanup_error, startup_process):
            return True, None
        self._reserve_close_locked()
        if self._starting or self._active_operations:
            return False, None
        return False, _CloseTargets(cleanup_error, startup_process, self._process)

    def _reserve_close_locked(self) -> None:
        """Claim the close before waiting, so nothing new starts behind it."""
        if not self._closing:
            self._closing = True
            self._condition.notify_all()

    def _close_attempt(self, deadline: float) -> tuple[bool, _CloseTargets | None]:
        self._acquire_state_lock(
            deadline,
            "Pyright close state lock deadline expired",
        )
        try:
            return self._close_attempt_locked()
        finally:
            self._lock.release()

    def _await_close_targets(self, deadline: float) -> _CloseTargets | None:
        """What to close, or None when this session was already closed."""
        while True:
            finished, targets = self._close_attempt(deadline)
            if finished:
                return None
            if targets is not None:
                return targets
            _sleep_before_close_retry(deadline)

    @staticmethod
    def _shut_down_targets(targets: _CloseTargets, deadline: float) -> None:
        """Close what this session owns, in the order it took them on."""
        try:
            _close_owned_targets(targets, deadline)
        except BaseException as error:
            _raise_collected_errors([], prior_error=error)

    def _forget_closed_owners_locked(self, targets: _CloseTargets) -> None:
        """Drop the owners we just closed, if they are still the current ones."""
        self._forget_closed_startup_locked(targets)
        self._sync_startup_atexit_locked()
        if self._process is targets.process:
            self._process = None
        self._bootstrap_owner_nonce = None

    def _forget_closed_startup_locked(self, targets: _CloseTargets) -> None:
        if self._startup_cleanup_error is targets.cleanup_error:
            self._startup_cleanup_error = None
        if self._startup_process is targets.startup_process:
            self._startup_process = None

    def _forget_session_state_locked(self) -> None:
        """Everything a running session accumulated is dropped on close."""
        self._reset_readiness_locked()
        self._documents.clear()
        self._document_bytes = 0
        self._document_identities.clear()
        self._document_used.clear()
        self._readiness_target_uri = None
        self._forget_generation_locked()
        self._progress_events.clear()
        self._progress_bytes = 0

    def _finish_close(self, targets: _CloseTargets, deadline: float) -> None:
        self._acquire_state_lock(
            deadline,
            "Pyright close final state lock deadline expired",
        )
        try:
            self._forget_closed_owners_locked(targets)
            self._forget_session_state_locked()
            self._closing = False
            self._closed = True
            self._condition.notify_all()
        finally:
            self._lock.release()

    def _close_owned(self, deadline: float) -> None:
        targets = self._await_close_targets(deadline)
        if targets is None:
            return
        self._shut_down_targets(targets, deadline)
        self._finish_close(targets, deadline)
