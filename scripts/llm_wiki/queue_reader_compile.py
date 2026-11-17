"""Adopted-queue compile requests, their recovery and their completion."""

from __future__ import annotations

import uuid
from contextlib import closing, contextmanager
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from llm_wiki.queue_artifacts import (
    copy_artifact_chunks,
    digest_stable_owner_file,
    harden_owner_only,
    read_stable_owner_file,
    write_durable_producer,
)
from llm_wiki.queue_contracts import MAX_RESULT_BYTES
from llm_wiki.queue_task_lineage import (
    compile_operator_progress,
    require_compile_root_work,
    require_compile_task_family,
    require_compile_work_family,
    require_dispatch_identity,
    require_uncertain_compile_dispatch,
    required_redrive_row,
)
from llm_wiki.queue_validation import (
    check_result_bytes,
    require_task_identifier,
)

if TYPE_CHECKING:
    pass
import compile_progress
from llm_wiki import queue_store
from llm_wiki.formats.canonical import sha256_bytes
from llm_wiki.queue_contracts import (
    LeaseFenceError,
    QueueLease,
    QueueOperationError,
)
from llm_wiki.queue_store import (
    check_claim_arguments,
    claim_task_filter,
    is_compile_recovery,
    require_active,
    settle_compile_recovery,
    unblock_in,
    v3_dedupe_match,
    validated_payload_bytes,
)
from llm_wiki.queue_timing import (
    timestamp,
)
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.sqlite_transactions import begin_immediate


def _unfinished_compile_request(database, payload_bytes, input_hash):
    existing = database.execute(
        """SELECT id, kind, handler_version, payload_blob, input_hash FROM tasks
           WHERE kind='compile' AND handler_version=1
             AND state IN ('ready','leased','blocked','dead')
             AND (input_hash=? OR payload_blob=?)
             AND NOT EXISTS (SELECT 1 FROM tasks AS child WHERE child.redrive_of=tasks.id)
           ORDER BY created_at, id LIMIT 1""", (input_hash, payload_bytes)).fetchone()
    if existing is None:
        return None
    return v3_dedupe_match(existing, 'compile', 1, payload_bytes, input_hash)


def _mark_compile_recovery(database, lease):
    changed = database.execute(
        """UPDATE tasks SET blocked_capability='provider_outcome_unknown',
           error_code='provider_outcome_unknown'
           WHERE id=? AND state='leased' AND lease_token=?""", (lease.id, lease.token),
    ).rowcount
    if changed != 1:
        raise LeaseFenceError('compile recovery claim lost its lease')


class QueueCompileOperations:
    def claim_compile_request(self, payload, *, owner,
                              lease_seconds=DEFAULTS.queue_lease_seconds,
                              max_attempts=DEFAULTS.queue_max_attempts,
                              deadline=float('inf'), cancelled=None):
        """Select/create and claim one intent atomically, without a steal window."""
        require_active(deadline, cancelled)
        check_claim_arguments(owner, lease_seconds, max_attempts)
        payload_bytes, input_hash = validated_payload_bytes(payload)
        with closing(self._connect(deadline=deadline)) as database, begin_immediate(database):
            task_id = self._compile_request_in(database, payload_bytes, input_hash)
            require_active(deadline, cancelled)
            lease = self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                   max_attempts=max_attempts, task_filter=claim_task_filter(task_id))
            require_active(deadline, cancelled)
            return task_id, lease

    def _compile_request_in(self, database, payload_bytes, input_hash):
        self._delete_stale_source_fences(database)
        self._assert_payload_not_fenced(database, payload_bytes.decode('utf-8'))
        existing = _unfinished_compile_request(database, payload_bytes, input_hash)
        if existing is not None:
            return existing
        task_id = uuid.uuid4().hex
        now = queue_store.utc_now()
        self._insert_task_row(database, task_id, 'compile', 1, payload_bytes,
                              input_hash, None, 0, now, now)
        return task_id

    def claim_uncertain_compile(
        self, owner: str, *, task_id: str, work_id: str, dispatch_digest: str,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
    ) -> QueueLease:
        """Acquire one uncertain dispatch atomically; this does not authorize a repeat.

        The operator entrypoint must hold its canonical queue owner, heartbeat the
        returned task lease and settle it. Normal payload, attempt and source
        fences remain in force. A failed acquisition leaves the task blocked.
        """
        check_claim_arguments(owner, lease_seconds, max_attempts)
        task_filter = claim_task_filter(task_id)
        compile_progress.require_digest(dispatch_digest)
        with closing(self._connect()) as database, begin_immediate(database):
            row = require_uncertain_compile_dispatch(database, task_id, work_id, dispatch_digest)
            if row['state'] == 'blocked':
                unblock_in(database, task_id, queue_store.utc_now())
            lease = self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                   max_attempts=max_attempts, task_filter=task_filter)
            if lease is None:
                raise QueueOperationError('compile_recovery_not_claimable')
            _mark_compile_recovery(database, lease)
            return lease

    def get_compile_event(self, work_id: str, *, sequence: int | None = None):
        """Read a checkpoint only after the explicit journal migration."""
        from compile_progress import event_at, latest_event

        with closing(self._connect()) as database:
            if sequence is not None:
                return event_at(database, work_id, sequence)
            return latest_event(database, work_id)

    def get_compile_dispatch(self, work_id: str):
        with closing(self._connect()) as database:
            return compile_progress.latest_dispatch(database, work_id)

    def inspect_compile_progress(self, task_id: str):
        """Read one consistent lineage/checkpoint snapshot without recovering work."""
        require_task_identifier(task_id)
        with closing(self._connect()) as database:
            database.execute('BEGIN')
            return compile_operator_progress(database, task_id)

    def verify_compile_checkpoint_owner(self, lease: QueueLease, checkpoint) -> None:
        """Reuse the lease/payload fence and immutable redrive lineage."""
        self._with_verified_lease(
            lease, partial(self._require_compile_checkpoint_owner, checkpoint=checkpoint),
        )

    def settle_compile_resolution(self, lease, dispatch, resolution, *, check_owner=None):
        """Revalidate the result, then release only this administrative lease."""
        from llm_wiki.compile_call_runtime import resolve_model_call

        if resolve_model_call(self, lease, dispatch) != resolution:
            raise compile_progress.ProgressConflict('compile resolution changed before settlement')
        if check_owner is not None:
            check_owner()
        outcome, event = resolution
        return self._with_verified_lease(
            lease, partial(self._settle_compile_resolution, event=event, outcome=outcome))

    @staticmethod
    def _settle_compile_resolution(database, row, now, *, event, outcome):
        if compile_progress.latest_event(database, event.work_id) != event:
            raise compile_progress.ProgressConflict('compile resolution changed before release')
        require_compile_task_family(database, row, event)
        state, history = {
            'answer_recovered': ('ready', 'succeeded'),
            'repeat_authorized': ('ready', 'succeeded'),
            'outcome_unknown': ('blocked', 'blocked'),
        }[outcome]
        settle_compile_recovery(database, row, now, state=state,
                                 outcome=history, code=f'compile_{outcome}')
        return state

    def abort_compile_recovery(self, lease):
        self._with_verified_lease(lease, self._abort_compile_recovery)

    @staticmethod
    def _abort_compile_recovery(database, row, now):
        settle_compile_recovery(database, row, now, state='blocked',
                                 outcome='blocked', code='compile_recovery_interrupted')

    def recover_expired_compile_recovery(self, task_id, dispatch):
        """Recover only the named administrative lease, never unrelated work."""
        with closing(self._connect()) as database, begin_immediate(database):
            row = required_redrive_row(database, task_id)
            require_compile_task_family(database, row, dispatch)
            now = queue_store.utc_now()
            if not is_compile_recovery(row) or row['lease_expires_at'] > timestamp(now):
                return
            require_dispatch_identity(compile_progress.latest_dispatch(database, dispatch.work_id),
                                       compile_progress.event_digest(dispatch))
            self._settle_expired_lease(database, row, now)

    @staticmethod
    def _require_compile_checkpoint_owner(database, row, now, *, checkpoint) -> None:
        require_compile_task_family(database, row, checkpoint)

    def record_compile_event(self, lease: QueueLease, event) -> None:
        """Use the same ownership and payload fence as terminal results."""
        from compile_progress import ProgressConflict, snapshot_event

        event = snapshot_event(event)
        if event.task_id != lease.id:
            raise ProgressConflict("compile checkpoint names another task")
        self._with_verified_lease(
            lease, partial(self._append_compile_event, event=event, lease=lease)
        )

    def save_compile_artifact(self, lease: QueueLease, event, data: bytes):
        """Durably store intermediate bytes before publishing their checkpoint.

        The existing queue byte bound is retained pending resource qualification;
        it is not a new limit or a claim that arbitrary model output is accepted.
        A fence loss after the file write leaves an unreferenced file, not progress.
        """
        check_result_bytes(data)
        return self.save_compile_artifact_stream(lease, event, (data,))

    def save_compile_artifact_stream(self, lease: QueueLease, event, chunks, *, deadline=None):
        """Persist an iterable without a total-size or in-memory-result ceiling."""
        return self.save_compile_artifact_writer(
            lease, event, partial(copy_artifact_chunks, chunks=chunks), deadline=deadline)

    def save_compile_artifact_writer(self, lease, event, producer, *, deadline=None):
        """Run a sequential format writer inside the same durable publication."""
        event = compile_progress.snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        self.results_dir.mkdir(parents=True, exist_ok=True)
        harden_owner_only(self.results_dir, 0o700)
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8')) + '.result'
        digest = write_durable_producer(self.results_dir / name, producer, deadline=deadline)
        saved = replace(event, details={**event.details, 'artifact': {
            'reference': f'run/queue-results/{name}', 'sha256': digest}})
        self.record_compile_event(lease, saved)
        return saved

    @contextmanager
    def open_compile_artifact(self, event, *, deadline=None):
        """A verified seekable file; the descriptor stays owned by this context."""
        relative, expected = compile_progress.artifact_identity(event)
        with self._open_compile_artifact_bytes(relative, expected, deadline) as handle:
            yield handle

    @staticmethod
    def _compile_artifact_operation(event) -> str:
        return f"compile:{event.work_id}:{event.sequence}"

    def recover_compile_artifact(self, lease: QueueLease, event, *, validate=None):
        """Adopt an atomic answer file left before the event transaction committed."""
        from compile_progress import snapshot_event

        event = snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8'))
        relative = f"run/queue-results/{name}.result"
        try:
            data = read_stable_owner_file(self.state_root / relative, MAX_RESULT_BYTES)
        except FileNotFoundError:
            return None
        if validate is not None:
            validate(data)
        saved = replace(event, details={
            **event.details, "artifact": {"reference": relative, "sha256": sha256_bytes(data)},
        })
        self.record_compile_event(lease, saved)
        return saved

    def recover_compile_artifact_stream(self, lease, event, *, expected_digest, deadline=None):
        """Adopt a complete orphan only when it matches the expected snapshot."""
        compile_progress.require_digest(expected_digest)
        saved = self._compile_orphan_event(lease, event, deadline)
        if saved is None:
            return None
        if saved.details['artifact']['sha256'] != expected_digest:
            raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
        self.record_compile_event(lease, saved)
        return saved

    def recover_compile_artifact_validated(self, lease, event, *, validate, deadline=None):
        """Adopt only after a caller proves the complete format's expected identity."""
        saved = self._compile_orphan_event(lease, event, deadline)
        if saved is None:
            return None
        with self.open_compile_artifact(saved, deadline=deadline) as handle:
            validate(handle)
        self.record_compile_event(lease, saved)
        return saved

    def _compile_orphan_event(self, lease, event, deadline):
        event = compile_progress.snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8'))
        relative = f'run/queue-results/{name}.result'
        try:
            digest, _ = digest_stable_owner_file(self.state_root / relative, deadline=deadline)
        except FileNotFoundError:
            return None
        return replace(event, details={**event.details, 'artifact': {
            'reference': relative, 'sha256': digest}})

    @staticmethod
    def _verify_compile_artifact_owner(database, row, now, *, event) -> None:
        from compile_progress import ProgressConflict

        if row["kind"] != "compile" or row["id"] != event.task_id:
            raise ProgressConflict("compile artifact does not belong to this task")
        require_compile_work_family(database, row, event)
        require_compile_root_work(database, row, event)
