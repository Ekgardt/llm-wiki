"""Helpers shared by test_compile_critic_drop_cannot_complete_source_work.py and the test files importing them."""

import json
from unittest.mock import Mock

from compile_cache import CompileCache
from llm_client import LLMResult
from llm_wiki import compile_pipeline_prompts, compile_pipeline_sources

from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import (
    _operation,
    _provider,
    _source_review,
)

QUOTE = 'Project Cedar merged change C after changes A and B.'


def _inputs(root):
    path = root / 'knowledge/daily/2026-07-14.md'
    path.write_text('## [10:00:00] session-end | manual\n' + QUOTE + '\n')
    return compile_pipeline_sources.snapshot_compile_inputs([path])


def _proposed(slug='cedar-merge-order'):
    operation = _operation('create', slug)
    operation['summary'] = 'A project-specific merge order was observed.'
    operation['body_markdown'] = 'Project Cedar merged change C after changes A and B.'
    operation['evidence'][0].update(quoted_text=QUOTE, claim=QUOTE)
    return operation


def _attempt(inputs, state, operations, verdicts):
    provider = _provider()
    attempt = compile_pipeline_prompts.CompileAttempt(inputs, CompileCache(state), None, None)
    response = json.dumps({'reviews': [dict(slug=item['slug'], verdict=verdict, reason='Review source-specific scope.')
                                      for item, verdict in zip(operations, verdicts)]})
    def reply(_descriptor, _prompt, _system, schema):
        text = response
        if schema == compile_pipeline_prompts.SOURCE_REVIEW_SCHEMA:
            text = _source_review()
        return LLMResult(provider, text, True, None, 'native')
    attempt._call = Mock(side_effect=reply)
    compile_pipeline_prompts.critique_prompt(inputs, operations)
    return attempt, provider
