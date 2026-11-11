"""Helpers shared by test_compile_work_completion.py and the test files importing them."""

import json
from types import SimpleNamespace

import compile_controller as controller
import compile_pipeline
import pytest
from llm_wiki import compile_pipeline_batches

from tests.compile_call_controller_helpers import work as work
from tests.compile_groups_complete_sources_by_qualified_capacity_helpers import MODEL
from tests.compile_paths import point_compile_path
from tests.compile_preflight_consumer_helpers import _dependencies, _snapshot
from tests.compile_repair_child_helpers import _child_backend
from tests.compile_repair_completion_helpers import _coordinator
from tests.repair_capacity_repartitions_complete_source_work_helpers import _batch


def _plan(reader, refused):
    for batch in reader.iter_batches(model=MODEL, on_refused=refused):
        yield from (child for child, _feedback in compile_pipeline.repartition_compile_repair(batch, ()))


@pytest.fixture
def multi(work, compiler_case, monkeypatch):
    batch, feedback = _batch(work[0].vault)
    snapshot = _snapshot(work, batch.inputs)
    original = compile_pipeline_batches.resolve_compile_plan
    operations = [item for record in feedback for item in json.loads(record)['operations']]
    calls = []

    def resolve(*args, **kwargs):
        dates = {part.logical_path.rsplit('/', 1)[1][:-3] for part in kwargs['batch'].inputs.dailies}
        selected = [item for item in operations if item['evidence'][0]['daily_date'] in dates]
        child = SimpleNamespace(feedback=(json.dumps({'operations': selected}),))
        calls.append(_child_backend(monkeypatch, child))
        return original(*args, **kwargs)

    point_compile_path(monkeypatch, 'resolve_compile_plan', resolve)
    return snapshot, calls


def _options(work, plan):
    return dict(coordinator=_coordinator(work), dependencies=_dependencies(), request={'model': MODEL},
                plan=plan)


def _confirm(work, multi, plan=_plan, **options):
    return controller.confirm_compile_work(work[0], work[1], multi[0], **_options(work, plan), **options)


def _execute(work, multi, plan=_plan, **options):
    return controller.execute_compile_work(work[0], work[1], multi[0], **_options(work, plan),
                                            trigger='manual', **options)


def _refuse_first(reader, refused):
    items = _plan(reader, refused)
    refused(next(items).inputs)
    yield from items
