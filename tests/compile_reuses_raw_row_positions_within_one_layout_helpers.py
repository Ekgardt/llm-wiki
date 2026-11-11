"""Helpers shared by test_compile_reuses_raw_row_positions_within_one_layout.py and the test files importing them."""

import compile_pipeline
import compile_receipts
from llm_wiki import compile_pipeline_batches, compile_pipeline_operations, compile_pipeline_prompts


def _bound_choices(inputs):
    choices = compile_pipeline_prompts.source_line_choices(inputs)
    return tuple(compile_pipeline_operations.evidence_binding(
        {'daily_date': row['daily_date'], 'timestamp': row['timestamp'],
         'quoted_text': row['quoted_text'], 'claim': 'A neutral fact.'}, inputs)
                 for row in choices)


def _context_pages():
    pages = []
    for index in range(12):
        content = f'Complete Cedar context {index}.\n'.encode()
        path = f'knowledge/notes/context-{index:02d}.md'
        pages.append(compile_receipts.SourceSnapshot(path, content, compile_pipeline.sha256_bytes(content)))
    return tuple(pages)


def _packing_result(inputs):
    batches = compile_pipeline_batches.pack_compile_batches(inputs, model=None)
    layouts = tuple(compile_pipeline_prompts.draft_layout(batch.inputs) for batch in batches)
    references = tuple(_bound_choices(batch.inputs) for batch in batches)
    return batches, layouts, references
