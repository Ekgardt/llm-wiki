"""Helpers shared by test_repair_capacity_repartitions_complete_source_work.py and the test files importing them."""

import json

from llm_wiki import compile_pipeline_batches, compile_pipeline_prompts, compile_pipeline_sources

from tests.compile_critic_drop_cannot_complete_source_work_helpers import _proposed


def _batch(root):
    paths = []
    operations = []
    for day in (14, 15, 16):
        date = f'2026-07-{day}'
        quote = f'Project Cedar recorded supported change {day}.'
        path = root / f'knowledge/daily/{date}.md'
        path.write_text(f'## [10:00:00] session-end | manual\n{quote}\n')
        paths.append(path)
        operation = _proposed(f'cedar-change-{day}')
        operation['body_markdown'] = quote
        operation['evidence'][0].update(daily_date=date, quoted_text=quote, claim=quote)
        operations.append(operation)
    note = root / 'knowledge/notes/context.md'
    note.write_text('---\ntype: pattern\n---\n# Context\nKeep the complete project context.\n')
    inputs = compile_pipeline_sources.snapshot_compile_inputs(paths)
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    reviews = [dict(slug=item['slug'], verdict='drop', reason='Correct its supported scope.')
               for item in operations]
    return batch, compile_pipeline_prompts.validated_critique_feedback(operations, reviews)


def _records(feedback, name):
    return sorted(json.dumps(item, sort_keys=True) for record in feedback
                  for item in json.loads(record)[name])


def _parts(children):
    return [part for child, _feedback in children for part in child.inputs.dailies]


def _combined_feedback(children):
    return tuple(record for _child, feedback in children for record in feedback)
