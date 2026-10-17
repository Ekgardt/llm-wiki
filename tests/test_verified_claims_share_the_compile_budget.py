"""A page preserves every source-bound claim within the complete response budget."""
import json

import compile_memory
import jsonschema
from claims import ClaimIndex, validate_claim_record
from markdown_transaction import MarkdownCoordinator

from tests.test_compile_claims_producer import DATE, _candidate, _draft, _evidence, _operation
from tests.test_compile_claims_producer import vault as vault


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
    jsonschema.validate(json.loads(_draft([operation])), compile_memory.RAW_PLAN_SCHEMA)


def test_normalized_page_preserves_every_verified_claim(vault):
    root, state = vault
    daily, operation = _nine_claim_operation(root)
    inputs = compile_memory.snapshot_compile_inputs([daily])
    operations = compile_memory._draft_operations(_draft([operation]))
    derived = compile_memory._with_derived_claims(operations, inputs)
    plan = compile_memory._normalize_plan(derived, inputs)
    content = json.loads(plan["operations"][0]["content"])
    records = content["claims"]
    assert len(records) == 9
    assert {record["subject"] for record in records} == {f"maintenance lease {number}" for number in range(9)}
    for record in records:
        validate_claim_record(record)
        compile_memory._require_claim_evidence(record, inputs)
    result = compile_memory.apply_compile_plan(inputs, plan, action_key="9" * 64, trigger="manual", coordinator=MarkdownCoordinator(root, state))
    assert result.state == "committed"
    index = ClaimIndex(state, vault=root)
    index.rebuild()
    for number in range(9):
        indexed = index.active_records(subject=f"maintenance lease {number}")
        assert len(indexed) == 1
        assert indexed[0].claim.record["evidence"]["text"] == f"Maintenance lease {number} expires after 30 seconds."
