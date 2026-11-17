from __future__ import annotations

import pytest
from claims import validate_claim_record
from llm_wiki import compile_pipeline_claims, compile_pipeline_operations, compile_pipeline_sources
from reliable_memory import sha256_bytes

from tests.compile_claims_producer_helpers import DATE, _candidate, _operation
from tests.compile_claims_producer_helpers import vault as vault


@pytest.mark.parametrize('line,partial,whole', (
    ('The maintenance lease expires after 30 seconds.', 'expires after 30',
     'The maintenance lease expires after 30 seconds.'),
    ('- The maintenance lease expires after 30 seconds.', 'expires after 30',
     'The maintenance lease expires after 30 seconds.'),
    ('    \u0410\u0440\u0435\u043d\u0434\u0430 \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043a\u0430\u0436\u0434\u044b\u0435 30 \u0441\u0435\u043a\u0443\u043d\u0434.', '\u043e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043a\u0430\u0436\u0434\u044b\u0435 30',
     '\u0410\u0440\u0435\u043d\u0434\u0430 \u0432\u043b\u0430\u0434\u0435\u043b\u044c\u0446\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043a\u0430\u0436\u0434\u044b\u0435 30 \u0441\u0435\u043a\u0443\u043d\u0434.'),
))
def test_widened_claim_text_matches_literal_hash_and_resolved_span(vault, line, partial, whole):
    root, _ = vault
    path = root / f'knowledge/daily/{DATE}.md'
    path.write_text(f'# Daily Session Memory — {DATE}\n\n## [10:00:00] session-end | manual\n{line}\n', encoding='utf-8')
    inputs = compile_pipeline_sources.snapshot_compile_inputs([path])
    operation = _operation([_candidate()])
    operation['evidence'][0]['quoted_text'] = partial
    record = compile_pipeline_operations.derived_claim(operation, _candidate(), inputs)
    validate_claim_record(record)
    compile_pipeline_claims.require_claim_evidence(record, inputs)
    assert record['text'] == whole
    assert record['evidence']['text'] == whole
    assert record['evidence']['sha256'] == sha256_bytes(whole.encode())


def test_fabricated_partial_quote_is_still_refused(vault):
    root, _ = vault
    path = root / f'knowledge/daily/{DATE}.md'
    path.write_text('## [10:00:00] session-end | manual\nThe actual source stays exact.\n', encoding='utf-8')
    inputs = compile_pipeline_sources.snapshot_compile_inputs([path])
    operation = _operation([_candidate()])
    operation['evidence'][0]['quoted_text'] = 'fabricated quote'
    with pytest.raises(ValueError, match='immutable snapshot'):
        compile_pipeline_operations.derived_claim(operation, _candidate(), inputs)
