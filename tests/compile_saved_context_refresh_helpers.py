"""Helpers shared by test_compile_saved_context_refresh.py and the test files importing them."""

from tests.compile_paths import point_compile_path


def _semantic_backend(monkeypatch):
    import json

    import llm_client as client
    from context_budget import TokenUsage

    from tests.compile_critic_drop_cannot_complete_source_work_helpers import _proposed
    from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import (
        _provider,
        _source_review,
    )

    provider = _provider()
    point_compile_path(monkeypatch, 'compile_candidate_chain', lambda _batch: [provider])
    point_compile_path(monkeypatch, 'probe_candidate', lambda _descriptor: True)
    replies = iter((json.dumps({'operations': [_proposed()]}),
        json.dumps({'reviews': [{'slug': 'cedar-merge-order', 'verdict': 'pass',
                                'reason': 'Supported by source.'}]}), _source_review()))
    calls = []

    def backend(*_args):
        calls.append(True)
        return client.BackendResponse(next(replies), TokenUsage(input_tokens=7, output_tokens=3))

    monkeypatch.setitem(client.BACKENDS, 'fake', backend)
    return calls
