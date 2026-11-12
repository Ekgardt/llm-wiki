"""Helpers shared by test_compile_preflight_consumer.py and the test files importing them."""

from dataclasses import replace

import compile_controller as controller
import pytest

from tests.compile_call_controller_helpers import work as work
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL
from tests.compile_streamed_archive_batches_helpers import _many_days


def _dependencies():
    return controller.CompileDependencies.capture(lambda: {'code': 'fixed'})


def _snapshot(work, inputs=None):
    queue, lease, _ = work
    inputs = inputs or _many_days(3)
    digest = controller.compile_inputs_digest(inputs)
    root = controller.begin_compile_work(queue, lease, lambda: (digest, _dependencies().digest, {'model': MODEL}))
    event = replace(root, sequence=1, stage='snapshot_ready', details={})
    return controller.save_compile_inputs(queue, lease, event, inputs)


def _plan(reader, refused):
    return reader.iter_batches(model=MODEL, on_refused=refused)


def _refused(_inputs):
    pytest.fail('unexpected refusal')


def _consume(work, snapshot, *, plan=_plan, on_refused=_refused, dependencies=None):
    return controller.iter_compile_preflight(work[0], work[1], snapshot, plan=plan,
        on_refused=on_refused, dependencies=dependencies or _dependencies(), request={'model': MODEL})
