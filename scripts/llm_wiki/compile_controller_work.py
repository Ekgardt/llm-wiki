"""Beginning, executing and confirming one whole compile work."""
from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field

import compile_progress as progress
from llm_wiki.compile_call_runtime import CompileDelivery
from llm_wiki.compile_controller_archive import snapshot_json
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.compile_controller_batch_admission import capture_current_compile_context
from llm_wiki.compile_controller_batches import (
    execute_planned_compile_tree,
    open_compile_batch_context,
)
from llm_wiki.compile_controller_preflight import (
    check_preflight_consumer,
    iter_compile_preflight,
    iter_saved_compile_preflight,
    save_compile_preflight,
)
from llm_wiki.compile_controller_publication import (
    append_or_match_compile_completion,
    compile_work_cursor,
    confirm_publication_under_gate,
    require_publication_coordinator,
)
from llm_wiki.compile_controller_reader import open_compile_inputs
from llm_wiki.compile_controller_repair import RepairProof


def execute_compile_work(queue, lease, snapshot, *, coordinator, dependencies, request, plan,
                         trigger, owner=None, deadline=None, cancelled=None,
                         capture=capture_current_compile_context):
    """Execute every retained root, then prove full coverage before returning success.

    Preflight refusals remain in the retained plan and prevent final proof. The
    worker parent owns heartbeat and queue settlement. Unknown calls block this
    delivery after independent roots; they never become successful work.
    """
    from compile_receipts import (
        require_compile_active,
    )

    require_publication_coordinator(queue, coordinator)
    require_compile_active(float('inf') if deadline is None else deadline, cancelled)
    items = iter_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
        request=request, plan=plan, on_refused=None, deadline=deadline)
    with CompileDelivery(queue, lease) as delivery, closing(items):
        for planned in items:
            execute_planned_compile_tree(queue, lease, planned, snapshot, coordinator=coordinator,
                dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
                cancelled=cancelled, delivery=delivery, capture=capture)
        return confirm_compile_work(queue, lease, snapshot, coordinator=coordinator,
            dependencies=dependencies, request=request, plan=plan, owner=owner,
            deadline=deadline, cancelled=cancelled)


def confirm_compile_work(queue, lease, snapshot, *, coordinator, dependencies, request, plan,
                         owner=None, deadline=None, cancelled=None):
    """Prove every root and refusal in one writer snapshot, without creating child work."""
    from compile_receipts import (
        require_compile_active,
    )
    from llm_wiki.compile_pipeline_receipts import (
        compile_writer_gate,
    )

    require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    require_compile_active(compiler_deadline, cancelled)
    preflight = save_compile_preflight(queue, lease, snapshot, dependencies=dependencies,
        request=request, plan=plan, deadline=deadline)
    with compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return _CompileWorkProof(queue, lease, snapshot, coordinator, dependencies,
                                 deadline, cancelled).run(preflight, plan)


@dataclass
class _CompileWorkProof:
    queue: object
    lease: object
    snapshot: progress.CompileEvent
    coordinator: object
    dependencies: object
    deadline: float | None
    cancelled: object
    batch_count: int = 0
    incomplete: bool = False
    digest: object = dataclass_field(default_factory=hashlib.sha256)

    def run(self, preflight, plan):
        items = iter_saved_compile_preflight(self.queue, self.lease, self.snapshot, preflight,
            self.dependencies, plan, self.refused, self.deadline)
        with closing(items):
            for planned in items:
                self._check_active()
                self._batch(planned)
        self._check_active()
        check_preflight_consumer(self.queue, self.lease, preflight, self.dependencies, self.deadline)
        return self._record(preflight)

    def refused(self, _inputs):
        self._check_active()
        self.incomplete = True

    def _check_active(self):
        from compile_receipts import (
            require_compile_active,
        )

        require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)

    def _batch(self, planned):
        self.batch_count += 1
        result = self._confirm_planned(planned)
        if result is None:
            self.incomplete = True
            return
        record = {'work_id': result.work_id, 'sequence': result.sequence,
                  'event_digest': progress.event_digest(result)}
        self.digest.update(snapshot_json(record) + b'\n')

    def _confirm_planned(self, planned):
        from llm_wiki.compile_pipeline_batches import (
            refresh_compile_batch_from_context,
        )

        saved = self.queue.get_compile_event(planned.work_id, sequence=1)
        if saved is None:
            return None
        with open_compile_batch_context(self.queue, self.lease, planned, self.snapshot, saved,
                dependencies=self.dependencies, deadline=self.deadline) as context:
            batch = refresh_compile_batch_from_context(planned.batch, context,
                deadline=float('inf') if self.deadline is None else self.deadline)
            return self._confirm_tree(saved, batch)

    def _confirm_tree(self, context, batch):
        cursor = compile_work_cursor(self.queue, context.work_id)
        if 'compile_repair' in cursor.details:
            return RepairProof(self.queue, self.lease, self.coordinator, self.dependencies,
                                self.deadline, self.cancelled).run(cursor, context, batch)
        if cursor.stage == 'publication_ready':
            return confirm_publication_under_gate(self.queue, self.lease, cursor, batch,
                self.coordinator, self.dependencies, self.deadline, self.cancelled)
        return None

    def _record(self, preflight):
        previous = self.queue.get_compile_event(preflight.work_id, sequence=preflight.sequence + 1)
        if self.incomplete:
            return _missing_work_completion(previous)
        proof = {'schema': 'compile-work-completion/v1', 'preflight_digest': progress.event_digest(preflight),
                 'batch_count': self.batch_count, 'children_digest': self.digest.hexdigest()}
        expected = replace(preflight, task_id=self.lease.id, sequence=preflight.sequence + 1,
                           stage='published', outcome='completed', details={'work_completion': proof})
        return append_or_match_compile_completion(self.queue, self.lease, previous, expected)


def _missing_work_completion(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded work completion has incomplete source receipts')
    return None


def load_compile_inputs(queue, lease, checkpoint):
    """Explicit eager compatibility for callers that require a complete value."""
    with open_compile_inputs(queue, lease, checkpoint) as snapshot:
        return snapshot.materialize()


def compile_root_details(origin, source_digest, dependency_digest, request):
    return progress.root_details(origin, source_digest, dependency_digest,
                                 compile_identity_digest(request))


def begin_compile_work(queue, lease, prepare):
    """Find retained original work before any live input/dependency preparation.

    prepare is called only for new work and returns source digest, dependency
    digest and invocation parameters. This never authorizes a model call itself.
    The existing queue transaction serializes publication of immutable event zero.
    """
    origin = queue.compile_origin(lease)
    work_id = progress.root_work_id(origin)
    previous = queue.get_compile_event(work_id, sequence=0)
    if previous is not None:
        queue.verify_compile_checkpoint_owner(lease, previous)
        progress.validate_root_event(previous, origin)
        return previous
    source_digest, dependency_digest, request = prepare()
    details = compile_root_details(origin, source_digest, dependency_digest, request)
    event = progress.CompileEvent(work_id, lease.id, 0, source_digest, 'pending', 'ready',
                                  {'compile_work': details})
    queue.record_compile_event(lease, event)
    return event
