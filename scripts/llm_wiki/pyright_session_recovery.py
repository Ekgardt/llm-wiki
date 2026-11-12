"""Deliver and commit a synchronize plan, and restart the server once when delivery breaks."""

import time
from dataclasses import dataclass
from pathlib import Path

from interruption import (
    interruption_in_chain as startup_interruption,
)
from interruption import (
    raise_collected_errors as _raise_collected_errors,
)
from llm_wiki.pyright_session_diagnostics import drop_superseded_diagnostics
from llm_wiki.pyright_session_documents import SyncPlan, without_wire_uri
from llm_wiki.pyright_session_values import (
    DiagnosticSnapshot,
    OpenDocument,
    validated_deadline,
)
from lsp_positions import path_to_file_uri
from lsp_process import LspProcess, ProcessState
from workspace_revision import WorkspaceDelta, WorkspaceRevision


@dataclass
class _WireAttempt:
    """Whether any notification of this pass has already reached the wire."""

    started: bool = False


class SessionRecovery:
    """Deliver a synchronize plan and recover from a broken delivery."""

    def _watched_uri(self, relative_path: str) -> str:
        absolute = Path(self._repository.checkout_root, relative_path)
        return path_to_file_uri(absolute)

    def _replay_generation_current(self, process: LspProcess) -> str | None:
        """The generation both we and the process agree is current, if any."""
        generation = self._generation_nonce
        if self._process is not process or generation is None:
            return None
        usable = (
            process.state not in {ProcessState.DEGRADED, ProcessState.FAILED}
            and generation == process.generation_nonce
        )
        return generation if usable else None

    def _synchronize_snapshot_replayed_locked(self, process: LspProcess) -> bool:
        generation = self._replay_generation_current(process)
        if generation is None or self._position_encoding is None:
            return False
        return all(
            self._wire_document_opened(document, generation)
            for document in self._documents.values()
        )

    def _recovery_unnecessary_locked(
        self, process: LspProcess, failed_generation: str
    ) -> bool:
        """Another generation already replayed the snapshot for us."""
        return (
            self._generation_nonce != failed_generation
            and self._synchronize_snapshot_replayed_locked(process)
        )

    def _forget_readiness_for_recovery_locked(self) -> None:
        self._readiness = "not_ready"
        self._readiness_evidence = ()
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0

    def _check_recovery_owners_locked(self, process: LspProcess) -> None:
        """Only the process we are recovering may be under our own ownership."""
        if self._process is not process:
            raise RuntimeError(
                "Pyright synchronization process changed before recovery"
            )
        if (
            self._startup_process is not None
            and self._startup_process is not process
        ):
            raise RuntimeError("Pyright synchronization cleanup owner is unavailable")

    def _take_recovery_ownership_locked(self, process: LspProcess) -> None:
        """Hold the process as a startup owner while it restarts."""
        self._process = None
        self._startup_process = process
        self._starting = True
        self._position_encoding = None
        self._capabilities = {}
        self._generation_nonce = None
        self._clear_wire_state()
        self._sync_startup_atexit_locked()
        self._condition.notify_all()

    def _claim_recovery(
        self, process: LspProcess, failed_generation: str
    ) -> tuple[bool, str | None]:
        """Whether this call owns the recovery, and the bootstrap owner it took."""
        with self._lock:
            if self._recovery_unnecessary_locked(process, failed_generation):
                return False, None
            self._forget_readiness_for_recovery_locked()
            self._check_recovery_owners_locked(process)
            bootstrap_owner_nonce = self._bootstrap_owner_nonce
            self._take_recovery_ownership_locked(process)
            return True, bootstrap_owner_nonce

    def _replayed_after_restart(self, process: LspProcess) -> bool:
        """Whether the restarted process proves the snapshot it had before."""
        with self._lock:
            if self._startup_process is not process or self._process is not None:
                return False
            self._process = process
            if self._synchronize_snapshot_replayed_locked(process):
                self._startup_process = None
                self._sync_startup_atexit_locked()
                return True
            self._process = None
            return False

    def _forget_recovery_state_locked(self) -> None:
        self._reset_readiness_locked()
        self._generation_nonce = None
        self._ready_uri_generations.clear()
        self._diagnostics.clear()
        self._diagnostic_bytes = 0
        self._clear_wire_state()

    def _abandon_recovery(
        self, process: LspProcess, bootstrap_owner_nonce: str | None
    ) -> None:
        """A recovery that failed leaves the process to be cleaned up, not used."""
        with self._lock:
            self._detach_recovered_process_locked(process, bootstrap_owner_nonce)
            self._forget_recovery_state_locked()
            self._sync_startup_atexit_locked()
            self._condition.notify_all()

    def _detach_recovered_process_locked(
        self, process: LspProcess, bootstrap_owner_nonce: str | None
    ) -> None:
        """Hand the process to the cleanup path and drop the owner that made it."""
        if self._process is process:
            self._process = None
        if self._startup_process is None:
            self._startup_process = process
        self._release_bootstrap_owner_locked(bootstrap_owner_nonce)

    def _release_bootstrap_owner_locked(self, owner_nonce: str | None) -> None:
        if self._bootstrap_owner_nonce == owner_nonce:
            self._bootstrap_owner_nonce = None

    def _release_recovery_serialization(self) -> None:
        with self._lock:
            self._starting = False
            self._condition.notify_all()

    def _recover_synchronize_snapshot(
        self,
        process: LspProcess,
        failed_generation: str,
        *,
        deadline: float,
    ) -> None:
        claimed, bootstrap_owner_nonce = self._claim_recovery(
            process, failed_generation
        )
        if not claimed:
            return
        try:
            process.restart(deadline)
            if self._replayed_after_restart(process):
                return
            raise RuntimeError(
                "Pyright synchronization recovery could not prove the prior snapshot"
            )
        except BaseException:
            self._abandon_recovery(process, bootstrap_owner_nonce)
            raise
        finally:
            self._release_recovery_serialization()

    def _notify_or_fail(
        self,
        plan: SyncPlan,
        method: str,
        params: dict[str, object],
        message: str,
    ) -> None:
        """Send one notification; an explicit refusal fails the synchronize."""
        delivered = plan.process.notify_generation(
            method,
            params,
            generation_nonce=plan.generation,
            deadline=plan.deadline,
        )
        if delivered is False:
            raise RuntimeError(message)

    def _send_change(
        self, plan: SyncPlan, document: OpenDocument, params: dict[str, object]
    ) -> None:
        """A changed document is opened once, then sent its change."""
        opened = self._send_did_open_once(
            document,
            plan.generation,
            deadline=plan.deadline,
            notify=lambda: plan.process.notify_generation(
                "textDocument/didOpen",
                self._did_open_params(document),
                generation_nonce=plan.generation,
                deadline=plan.deadline,
            ),
        )
        if not opened:
            raise RuntimeError("Pyright didOpen notification was not delivered")
        self._notify_or_fail(
            plan,
            "textDocument/didChange",
            params,
            "Pyright didChange notification was not delivered",
        )

    def _send_synchronize_notifications(
        self, plan: SyncPlan, attempt: _WireAttempt
    ) -> None:
        for _document, params in plan.close_notifications:
            attempt.started = True
            self._notify_or_fail(
                plan,
                "textDocument/didClose",
                params,
                "Pyright didClose notification was not delivered",
            )
        for document, _replacement, params in plan.changed_replacements:
            attempt.started = True
            self._send_change(plan, document, params)
        if plan.watched_params is not None:
            attempt.started = True
            self._notify_or_fail(
                plan,
                "workspace/didChangeWatchedFiles",
                plan.watched_params,
                "Pyright watched-files notification was not delivered",
            )

    def _recover_after_wire_failure(
        self, plan: SyncPlan, notification_error: BaseException
    ) -> None:
        """Put the server back to the snapshot it had before this pass."""
        try:
            self._recover_synchronize_snapshot(
                plan.process,
                plan.generation,
                deadline=plan.deadline,
            )
        except BaseException as recovery_error:
            if (
                startup_interruption(notification_error) is not None
                or startup_interruption(recovery_error) is not None
            ):
                _raise_collected_errors(
                    [recovery_error],
                    prior_error=notification_error,
                )
            raise RuntimeError(
                "Pyright synchronization notification recovery failed"
            ) from recovery_error

    def _deliver_synchronize(self, plan: SyncPlan) -> None:
        """Send the planned notifications, restoring the snapshot if one fails."""
        attempt = _WireAttempt()
        try:
            self._send_synchronize_notifications(plan, attempt)
        except BaseException as notification_error:
            if attempt.started:
                self._recover_after_wire_failure(plan, notification_error)
            raise

    def _documents_unchanged_locked(
        self, snapshot: dict[str, OpenDocument]
    ) -> bool:
        """No document was opened, closed or replaced while the plan was built."""
        if len(self._documents) != len(snapshot):
            return False
        return all(
            self._documents.get(uri) is document
            for uri, document in snapshot.items()
        )

    def _commit_identity_changed_locked(self, plan: SyncPlan) -> bool:
        """The process, generation or revision the plan was built on has moved."""
        return (
            self._process is not plan.process
            or self._workspace_revision is not plan.prior
            or self._generation_nonce != plan.generation
            or plan.process.generation_nonce != plan.generation
        )

    def _commit_state_changed_locked(self, plan: SyncPlan) -> bool:
        if self._closed or self._closing:
            return True
        if self._commit_identity_changed_locked(plan):
            return True
        return not self._documents_unchanged_locked(plan.documents_snapshot)

    def _projected_readiness(
        self, plan: SyncPlan
    ) -> tuple[dict[str, DiagnosticSnapshot], dict[str, str], str | None]:
        """Diagnostics, readiness and target after the planned closes and changes."""
        next_diagnostics = dict(self._diagnostics)
        next_ready = dict(self._ready_uri_generations)
        next_target = self._readiness_target_uri
        for document in plan.closed_documents:
            next_ready.pop(document.source.uri, None)
            next_diagnostics.pop(document.source.uri, None)
            if next_target == document.source.uri:
                next_target = None
        for document, replacement, _params in plan.changed_replacements:
            drop_superseded_diagnostics(next_diagnostics, document, replacement)
        return next_diagnostics, next_ready, next_target

    def _projected_wire_state(
        self, plan: SyncPlan
    ) -> tuple[set[tuple[object, ...]], set[tuple[object, ...]]]:
        """The wire bookkeeping after the planned closes and version bumps."""
        opened = set(self._wire_opened)
        failed = set(self._wire_failed)
        for document in plan.closed_documents:
            uri = document.source.uri
            opened = without_wire_uri(opened, plan.generation, uri)
            failed = without_wire_uri(failed, plan.generation, uri)
        for document, replacement, _params in plan.changed_replacements:
            opened.discard((plan.generation, document.source.uri, document.version))
            opened.add(
                (plan.generation, replacement.source.uri, replacement.version)
            )
        return opened, failed

    def _relax_readiness_locked(
        self, next_documents: dict[str, OpenDocument], next_target: str | None
    ) -> None:
        """With no readiness target left, the session falls back to initialized."""
        if next_target is not None or self._readiness != "query_ready":
            return
        self._readiness = "protocol_initialized"
        self._readiness_evidence = (
            "initialize",
            "initialized",
            "configuration",
            *(("didOpen",) if next_documents else ()),
        )

    def _projected_document_identities(
        self, plan: SyncPlan
    ) -> dict[str, tuple[int, int, int, int]]:
        """The file identities that still describe the content we will hold."""
        replaced = {
            document.source.uri for document, _next, _params in plan.changed_replacements
        }
        kept = set(plan.next_documents) - replaced
        return {
            uri: identity
            for uri, identity in self._document_identities.items()
            if uri in kept
        }

    def _apply_synchronize_commit_locked(self, plan: SyncPlan) -> None:
        """Publish the planned state; the caller holds the session and wire locks."""
        next_diagnostics, next_ready, next_target = self._projected_readiness(plan)
        self._wire_opened, self._wire_failed = self._projected_wire_state(plan)
        self._wire_condition.notify_all()
        self._documents = plan.next_documents
        self._document_bytes = plan.projected_document_bytes
        self._document_identities = self._projected_document_identities(plan)
        self._document_used = {
            uri: used
            for uri, used in self._document_used.items()
            if uri in plan.next_documents
        }
        self._ready_uri_generations = next_ready
        self._diagnostics = next_diagnostics
        self._diagnostic_bytes = sum(
            snapshot.retained_bytes for snapshot in next_diagnostics.values()
        )
        self._readiness_target_uri = next_target
        self._relax_readiness_locked(plan.next_documents, next_target)
        self._workspace_revision = plan.revision
        self._condition.notify_all()

    def _commit_synchronize(self, plan: SyncPlan) -> RuntimeError | None:
        """Publish the planned state, or say why it could not be published."""
        with self._lock:
            if self._commit_state_changed_locked(plan):
                return RuntimeError(
                    "Pyright synchronization state changed before commit"
                )
            return self._commit_wire_generation_locked(plan)

    def _commit_wire_generation_locked(self, plan: SyncPlan) -> RuntimeError | None:
        with self._wire_condition:
            if self._wire_generation != plan.generation:
                return RuntimeError(
                    "Pyright synchronization generation changed before commit"
                )
            self._apply_synchronize_commit_locked(plan)
        return None

    def _recover_after_commit_failure(
        self, plan: SyncPlan, commit_error: RuntimeError
    ) -> None:
        """Restore the server's snapshot, then report why the commit failed."""
        try:
            self._recover_synchronize_snapshot(
                plan.process,
                plan.generation,
                deadline=plan.deadline,
            )
        except BaseException as recovery_error:
            if startup_interruption(recovery_error) is not None:
                _raise_collected_errors(
                    [recovery_error],
                    prior_error=commit_error,
                )
            raise RuntimeError(
                "Pyright synchronization commit recovery failed"
            ) from recovery_error
        raise commit_error

    def synchronize(
        self,
        revision: WorkspaceRevision,
        *,
        deadline: float,
    ) -> WorkspaceDelta:
        deadline = validated_deadline(deadline)
        self._check_synchronize_revision(revision, deadline)
        with (
            self._document_operation_lock(deadline),
            self._operation(),
            self._synchronize_semantic_fence(),
        ):
            self.start(deadline=deadline)
            plan, delta = self._synchronize_plan(revision, deadline=deadline)
            self._deliver_synchronize(plan)
            commit_error = self._commit_synchronize(plan)
            if commit_error is not None:
                self._recover_after_commit_failure(plan, commit_error)
            return delta

    def _check_synchronize_revision(
        self, revision: WorkspaceRevision, deadline: float
    ) -> None:
        self._require_matching_revision(revision)
        if time.monotonic() >= deadline:
            raise TimeoutError("Pyright synchronize deadline expired")

    def _require_matching_revision(self, revision: WorkspaceRevision) -> None:
        if not isinstance(revision, WorkspaceRevision):
            raise TypeError("revision must be a WorkspaceRevision")
        if (
            revision.repository_id != self._repository.repository_id
            or revision.checkout_id != self._repository.checkout_id
        ):
            raise ValueError("workspace revision must describe this checkout")
