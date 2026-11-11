"""Helpers shared by test_compile_repair_child.py and the test files importing them."""

import json

import llm_client
from context_budget import TokenUsage

from tests.compile_paths import point_compile_path
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import (
    _provider,
    _source_review,
)


def _child_backend(monkeypatch, child):
    operations = [operation for record in child.feedback for operation in json.loads(record)['operations']]
    replies = iter((json.dumps({'operations': operations}),
        json.dumps({'reviews': [dict(slug=item['slug'], verdict='pass', reason='Source supports correction.')
                                for item in operations]}), _source_review()))
    calls = []

    def backend(*args):
        calls.append(True)
        return llm_client.BackendResponse(next(replies), TokenUsage(input_tokens=7, output_tokens=3))

    point_compile_path(monkeypatch, 'compile_candidate_chain', lambda batch: [_provider()])
    point_compile_path(monkeypatch, 'probe_candidate', lambda descriptor: True)
    monkeypatch.setitem(llm_client.BACKENDS, 'fake', backend)
    return calls
