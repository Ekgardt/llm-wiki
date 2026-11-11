"""A repair validates all proposals without reparsing identical source bytes."""
from collections import Counter

import compile_pipeline
import evidence_resolver
from llm_wiki import compile_pipeline_claims

from tests.repair_capacity_repartitions_complete_source_work_helpers import (
    _batch,
    _combined_feedback,
    _parts,
    _records,
)
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import vault as vault


def test_repair_parses_each_held_immutable_layout_once(vault, monkeypatch):
    batch, feedback = _batch(vault[0])
    parsed = Counter()
    original = evidence_resolver.daily_entries

    def counted(content):
        parsed[id(content)] += 1
        return original(content)

    monkeypatch.setattr(evidence_resolver, 'daily_entries', counted)

    children = compile_pipeline.repartition_compile_repair(batch, feedback)

    assert len(children) == 2
    assert _parts(children) == list(batch.inputs.dailies)
    combined = _combined_feedback(children)
    assert _records(combined, 'operations') == _records(feedback, 'operations')
    assert _records(combined, 'reviews') == _records(feedback, 'reviews')
    assert parsed
    assert max(parsed.values()) == 1, dict(parsed)
    assert compile_pipeline_claims.SOURCE_CHOICE_RESOLVER.get() is None

    repeated = compile_pipeline.repartition_compile_repair(batch, feedback)

    assert _records(_combined_feedback(repeated), 'operations') == _records(feedback, 'operations')
    assert max(parsed.values()) == 2, dict(parsed)
    assert compile_pipeline_claims.SOURCE_CHOICE_RESOLVER.get() is None
