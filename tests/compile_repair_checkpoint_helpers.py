"""Helpers shared by test_compile_repair_checkpoint.py and the test files importing them."""

import compile_controller as controller
import pytest
from llm_wiki import compile_pipeline_batches

from tests.compile_call_controller_helpers import work as work
from tests.compile_preflight_consumer_helpers import _consume, _dependencies, _snapshot
from tests.repair_capacity_repartitions_complete_source_work_helpers import _batch


@pytest.fixture
def repair(work, compiler_case):
    original, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, original.inputs)
    items = _consume(work, snapshot)
    try:
        with controller.open_prepared_compile_batch(work[0], work[1], next(items), snapshot,
                dependencies=_dependencies()) as (batch, context):
            yield batch, context, compile_pipeline_batches.CompileRepairCapacityError('input_capacity', feedback)
    finally:
        items.close()


def _save(work, repair):
    batch, context, failure = repair
    return controller.save_compile_repair(work[0], work[1], context, batch, failure,
        dependencies=_dependencies())


def _read(work, repair, saved):
    return controller.read_compile_repair(work[0], work[1], saved, repair[1], repair[0],
        dependencies=_dependencies())
