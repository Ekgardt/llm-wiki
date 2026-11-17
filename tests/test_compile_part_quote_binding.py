"""A timestamp candidate must contain the quoted bytes before it can bind."""
import compile_pipeline
import compile_receipts
import evidence_resolver
import pytest
from llm_wiki import compile_pipeline_operations


def _parts(bodies):
    chunks = [b'## [11:12:12] event\n' + body + b'\n' for body in bodies]
    original = b''.join(chunks)
    entries = tuple(evidence_resolver.daily_entries(original))
    offset = 0
    parts = []
    for index, chunk in enumerate(chunks):
        parts.append(compile_receipts.DailySnapshot(
            'knowledge/daily/2026-10-18.md', chunk, compile_pipeline.sha256_bytes(chunk),
            part_index=index, part_count=len(chunks), byte_start=offset,
            byte_end=offset + len(chunk), original_content=original,
            original_sha256=compile_pipeline.sha256_bytes(original), original_entries=entries,
        ))
        offset += len(chunk)
    return parts


@pytest.mark.parametrize('selected', [0, 1])
def test_only_quote_bearing_part_binds_repeated_timestamp(selected):
    bodies = [b'First exact fact.', b'Second exact fact.']
    parts = _parts(bodies)
    source, block, start = compile_pipeline_operations.bound_part(parts, '11:12:12', bodies[selected])
    assert source is parts[selected]
    assert start == parts[selected].byte_start
    assert bodies[selected] in block


@pytest.mark.parametrize('bodies', [
    [b'The same fact.', b'The same fact.'],
    [b'Another fact.', b'Other evidence.'],
    [b'The same fact. The same fact.', b'Other evidence.'],
])
def test_missing_or_repeated_quote_does_not_gain_authority(bodies):
    with pytest.raises(ValueError, match='ambiguous or missing'):
        compile_pipeline_operations.bound_part(_parts(bodies), '11:12:12', b'The same fact.')
