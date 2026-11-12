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
    ('    Аренда владельца обновляется каждые 30 секунд.', 'обновляется каждые 30',
     'Аренда владельца обновляется каждые 30 секунд.'),
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
