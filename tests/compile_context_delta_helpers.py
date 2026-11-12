"""Helpers shared by test_compile_context_delta.py and the test files importing them."""

import compile_receipts
from llm_wiki import compile_pipeline_sources
from reliable_memory import sha256_bytes


def _source(path, body):
    return compile_receipts.SourceSnapshot(path, body, sha256_bytes(body))


def _context(body=b'accepted note'):
    source = _source('knowledge/notes/accepted.md', body)
    target = compile_pipeline_sources.TargetSnapshot(source.logical_path, body, source.sha256)
    rule = _source('AGENTS.md', b'operating contract')
    return compile_pipeline_sources.CompileInputs((), (rule, source), (target,), (rule,))


def _records(inputs):
    for group in ('sources', 'targets', 'vault_files'):
        for record in getattr(inputs, group):
            yield group, record


def _materialized(view):
    return compile_pipeline_sources.CompileInputs((), tuple(view.sources), tuple(view.targets), tuple(view.vault_files))
