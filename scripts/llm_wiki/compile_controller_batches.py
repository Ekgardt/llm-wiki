"""Executing planned batches and the compile tree they form."""
from __future__ import annotations

import zipfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass

import compile_progress as progress
from llm_wiki.compile_call_runtime import CompileDelivery, ProviderOutcomeUnknown
from llm_wiki.compile_controller_batch_admission import (
    batch_context_event,
    capture_current_compile_context,
    context_event_binding,
    read_bound_batch_context,
    save_compile_batch_context,
    validate_context_view,
)
from llm_wiki.compile_controller_capture import require_capture_event_identity
from llm_wiki.compile_controller_publication import (
    check_batch_context_owner,
    compile_work_cursor,
    confirm_compile_publication,
    require_publication_coordinator,
)
from llm_wiki.compile_controller_reader import open_compile_inputs
from llm_wiki.compile_controller_repair import (
    RepairCompileBatch,
    begin_compile_repair_batch,
    confirm_compile_repair,
    execute_context_compile_batch,
    read_compile_repair,
)


@contextmanager
def open_compile_batch_context(queue, lease, planned, snapshot, saved, *, dependencies, deadline=None):
    """Yield the full saved context under both archive lifetimes and owner checks."""
    expected = batch_context_event(queue, lease, planned, snapshot, dependencies, deadline)
    recorded = queue.get_compile_event(expected.work_id, sequence=expected.sequence)
    if recorded != saved:
        raise progress.ProgressConflict('batch context requires the recorded checkpoint')
    require_capture_event_identity(expected, saved)
    with (
        open_compile_inputs(queue, lease, snapshot, deadline=deadline) as base,
        queue.open_compile_artifact(saved, deadline=deadline) as handle,
        zipfile.ZipFile(handle) as archive,
    ):
        view = read_bound_batch_context(archive, base, context_event_binding(expected))
        validate_context_view(view, deadline)
        check_batch_context_owner(queue, lease, saved, dependencies, deadline)
        yield view
        check_batch_context_owner(queue, lease, saved, dependencies, deadline)


@contextmanager
def open_prepared_compile_batch(queue, lease, planned, snapshot, *, dependencies,
                                capture=capture_current_compile_context, deadline=None):
    """Prepare from retained context while its lazy source/target views stay open."""
    from llm_wiki.compile_pipeline_batches import (
        refresh_compile_batch_from_context,
    )

    saved = save_compile_batch_context(queue, lease, planned, snapshot,
        dependencies=dependencies, capture=capture, deadline=deadline)
    with open_compile_batch_context(queue, lease, planned, snapshot, saved,
                                    dependencies=dependencies, deadline=deadline) as context:
        compiler_deadline = float('inf') if deadline is None else deadline
        ready = refresh_compile_batch_from_context(planned.batch, context, deadline=compiler_deadline)
        check_batch_context_owner(queue, lease, saved, dependencies, deadline)
        yield ready, saved


def execute_planned_compile_batch(queue, lease, planned, snapshot, *, coordinator, dependencies,
                                  trigger, owner=None, deadline=None, cancelled=None, delivery=None,
                                  capture=capture_current_compile_context):
    """Resolve and publish one retained batch; never settle its enclosing task.

    Repair-capacity and unknown-outcome errors propagate to the delivery owner.
    The worker remains responsible for whole-call process supervision.
    """
    from compile_receipts import (
        require_compile_active,
    )

    require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    require_compile_active(compiler_deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    with open_prepared_compile_batch(queue, lease, planned, snapshot, dependencies=dependencies,
                                     capture=capture, deadline=deadline) as (batch, context):
        return execute_context_compile_batch(queue, lease, context, batch, coordinator=coordinator,
            dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
            cancelled=cancelled, delivery=delivery, critic_feedback=())


def execute_planned_compile_tree(queue, lease, planned, snapshot, *, coordinator, dependencies,
                                 trigger, owner=None, deadline=None, cancelled=None, delivery=None,
                                 capture=capture_current_compile_context):
    """Own the retained archive while executing a batch and its saved repairs."""
    from compile_receipts import (
        require_compile_active,
    )

    require_publication_coordinator(queue, coordinator)
    require_compile_active(float('inf') if deadline is None else deadline, cancelled)
    if delivery is not None:
        delivery.require_owner(queue, lease)
    with open_prepared_compile_batch(queue, lease, planned, snapshot, dependencies=dependencies,
                                     capture=capture, deadline=deadline) as (batch, context):
        return execute_compile_tree(queue, lease, context, batch, coordinator=coordinator,
            dependencies=dependencies, trigger=trigger, owner=owner, deadline=deadline,
            cancelled=cancelled, delivery=delivery)


def execute_compile_tree(queue, lease, context, batch, *, coordinator, dependencies, trigger,
                         owner=None, deadline=None, cancelled=None, delivery=None):
    """Execute disjoint retained source branches; incomplete work never proves success.

    The caller owns the open archive. An owned delivery blocks on unknown calls
    after independent siblings; a supplied delivery retains that obligation.
    Capacity refusals and recorded resolution failures return no completion.
    Other failures propagate. This does not settle the enclosing queue task.
    """
    require_publication_coordinator(queue, coordinator)
    scope = CompileDelivery(queue, lease) if delivery is None else nullcontext(delivery)
    with scope as active:
        active.require_owner(queue, lease)
        return _CompileTreeExecution(queue, lease, coordinator, dependencies, trigger,
            owner, deadline, cancelled, active).run(context, batch)


@dataclass(frozen=True)
class _CompileTreeFrame:
    context: progress.CompileEvent
    batch: object
    feedback: tuple = ()
    child: RepairCompileBatch | None = None


@dataclass
class _CompileTreeExecution:
    queue: object
    lease: object
    coordinator: object
    dependencies: object
    trigger: str
    owner: object
    deadline: float | None
    cancelled: object
    delivery: CompileDelivery

    def run(self, context, batch):
        stack = [_CompileTreeFrame(context, batch)]
        while stack:
            self._check_active()
            frame = self._admit(stack.pop())
            self._visit(frame, stack)
        return self._confirm(context, batch)

    def _check_active(self):
        from compile_receipts import (
            require_compile_active,
        )

        require_compile_active(float('inf') if self.deadline is None else self.deadline, self.cancelled)

    def _admit(self, frame):
        if frame.child is None:
            return frame
        context = begin_compile_repair_batch(self.queue, self.lease, frame.child, frame.context, frame.batch,
            dependencies=self.dependencies, deadline=self.deadline)
        return _CompileTreeFrame(context, frame.child.batch, frame.child.feedback)

    def _visit(self, frame, stack):
        from llm_wiki.compile_pipeline_batches import (
            CompilePlanUnavailable,
            CompileRepairCapacityError,
        )

        try:
            execute_context_compile_batch(self.queue, self.lease, frame.context, frame.batch,
                coordinator=self.coordinator, dependencies=self.dependencies, trigger=self.trigger,
                owner=self.owner, deadline=self.deadline, cancelled=self.cancelled,
                delivery=self.delivery, critic_feedback=frame.feedback)
        except CompileRepairCapacityError:
            self._expand(frame, stack)
        except ProviderOutcomeUnknown:
            # CompileDelivery retains uncertainty; final proof cannot complete this leaf.
            return
        except CompilePlanUnavailable as failure:
            self._record_resolution_failure(frame, failure)

    def _record_resolution_failure(self, frame, failure):
        from compile_pipeline import (
            record_failed_batch,
        )

        self._check_active()
        check_batch_context_owner(self.queue, self.lease, frame.context,
                                   self.dependencies, self.deadline)
        record_failed_batch(frame.batch.inputs, failure,
                             deadline=float('inf') if self.deadline is None else self.deadline)

    def _expand(self, frame, stack):
        repair = compile_work_cursor(self.queue, frame.context.work_id)
        children = read_compile_repair(self.queue, self.lease, repair, frame.context, frame.batch,
            dependencies=self.dependencies, deadline=self.deadline)
        for child in reversed(children):
            stack.append(_CompileTreeFrame(frame.context, frame.batch, child=child))

    def _confirm(self, context, batch):
        self._check_active()
        cursor = compile_work_cursor(self.queue, context.work_id)
        options = dict(coordinator=self.coordinator, dependencies=self.dependencies,
                       owner=self.owner, deadline=self.deadline, cancelled=self.cancelled)
        if 'compile_repair' in cursor.details:
            return confirm_compile_repair(self.queue, self.lease, cursor, context, batch, **options)
        if cursor.stage == 'publication_ready':
            return confirm_compile_publication(self.queue, self.lease, cursor, batch, **options)
        return None
