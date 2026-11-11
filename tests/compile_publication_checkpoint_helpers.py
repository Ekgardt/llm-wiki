"""Helpers shared by test_compile_publication_checkpoint.py and the test files importing them."""

from contextlib import contextmanager

import compile_controller as controller
from llm_wiki import compile_pipeline_apply, compile_pipeline_batches

from tests.compile_call_controller_helpers import work as work
from tests.compile_preflight_consumer_helpers import _consume, _dependencies, _snapshot
from tests.compile_saved_context_refresh_helpers import _semantic_backend


@contextmanager
def _resolved(work, case, monkeypatch):
    from compile_cache import CompileCache
    from markdown_transaction import active_markdown_coordinator

    inputs, state = case
    snapshot = _snapshot(work, inputs)
    items = _consume(work, snapshot)
    item = next(items)
    calls = _semantic_backend(monkeypatch)
    coordinator = active_markdown_coordinator(work[0].vault, state)
    with controller.open_prepared_compile_batch(work[0], work[1], item, snapshot,
            dependencies=_dependencies()) as (ready, context):
        replay = controller.CompileReplay(work[0], work[1], context, dependencies=_dependencies())
        resolved = compile_pipeline_batches.resolve_compile_plan(ready.inputs, CompileCache(state),
            coordinator=coordinator, batch=ready, call_runner=replay)
        yield ready, context, resolved, coordinator, calls
    items.close()


def _save(work, ready, context, resolved):
    return controller.save_compile_publication(work[0], work[1], context, ready, resolved,
        dependencies=_dependencies())


def _apply_saved(work, ready, saved, coordinator):
    document = controller.read_compile_publication(work[0], work[1], saved)
    return compile_pipeline_apply.apply_compile_plan(ready.inputs, document['plan'],
        action_key=document['action_key'], trigger='manual', coordinator=coordinator,
        batch=ready, provider_budget=document['provider_budget'], completed_at=document['completed_at'])
