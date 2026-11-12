"""Helpers shared by test_compile_tree_execution.py and the test files importing them."""

import compile_controller as controller
import pytest
from llm_wiki import compile_pipeline_batches

from tests.compile_call_controller_helpers import work as work
from tests.compile_preflight_consumer_helpers import _consume, _dependencies, _snapshot
from tests.compile_repair_checkpoint_helpers import _read, _save
from tests.compile_repair_checkpoint_helpers import repair as repair
from tests.compile_repair_completion_helpers import _coordinator
from tests.repair_capacity_repartitions_complete_source_work_helpers import _batch


@pytest.fixture
def saved_tree(work, compiler_case):
    original, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, original.inputs)
    items = _consume(work, snapshot)
    try:
        planned = next(items)
        with controller.open_prepared_compile_batch(work[0], work[1], planned, snapshot,
                dependencies=_dependencies()) as (batch, context):
            repair = batch, context, compile_pipeline_batches.CompileRepairCapacityError('capacity', feedback)
            saved = _save(work, repair)
            children = _read(work, repair, saved)
        yield planned, snapshot, saved, children
    finally:
        items.close()


def _execute_planned(work, saved_tree, **options):
    return controller.execute_planned_compile_tree(work[0], work[1], saved_tree[0], saved_tree[1],
        coordinator=_coordinator(work), dependencies=_dependencies(), trigger='manual', **options)
