"""A page preserves every source-bound claim within the complete response budget."""
import json

import jsonschema
from claims import ClaimIndex, validate_claim_record
from llm_wiki import (
    compile_pipeline_apply,
    compile_pipeline_base,
    compile_pipeline_claims,
    compile_pipeline_operations,
    compile_pipeline_prompts,
    compile_pipeline_sources,
)
from markdown_transaction import MarkdownCoordinator

from tests.compile_claims_producer_helpers import DATE, _candidate, _draft, _evidence, _operation
from tests.compile_claims_producer_helpers import vault as vault


def _nine_claim_operation(root):
    quotes = [f"Maintenance lease {number} expires after 30 seconds." for number in range(9)]
    daily = root / f"knowledge/daily/{DATE}.md"
    daily.write_text(f"# Daily Session Memory — {DATE}\n\n## [10:00:00] session-end | manual\n" + "\n".join(quotes) + "\n", encoding="utf-8")
    operation = _operation([_candidate(subject=f"maintenance lease {number}", evidence_index=number) for number in range(9)])
    operation["evidence"] = [_evidence(quote) for quote in quotes]
    return daily, operation


def test_provider_schema_accepts_nine_source_bound_claims(vault):
    root, _state = vault
    _daily, operation = _nine_claim_operation(root)
    jsonschema.validate(json.loads(_draft([operation])), compile_pipeline_base.RAW_PLAN_SCHEMA)


def test_normalized_page_preserves_every_verified_claim(vault):
    root, state = vault
    daily, operation = _nine_claim_operation(root)
    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    operations = compile_pipeline_prompts.draft_operations(_draft([operation]))
    derived = compile_pipeline_operations.with_derived_claims(operations, inputs)
    plan = compile_pipeline_prompts.normalize_plan(derived, inputs)
    content = json.loads(plan["operations"][0]["content"])
    records = content["claims"]
    assert len(records) == 9
    assert {record["subject"] for record in records} == {f"maintenance lease {number}" for number in range(9)}
    for record in records:
        validate_claim_record(record)
        compile_pipeline_claims.require_claim_evidence(record, inputs)
    result = compile_pipeline_apply.apply_compile_plan(inputs, plan, action_key="9" * 64, trigger="manual", coordinator=MarkdownCoordinator(root, state))
    assert result.state == "committed"
    index = ClaimIndex(state, vault=root)
    index.rebuild()
    for number in range(9):
        indexed = index.active_records(subject=f"maintenance lease {number}")
        assert len(indexed) == 1
        assert indexed[0].claim.record["evidence"]["text"] == f"Maintenance lease {number} expires after 30 seconds."
