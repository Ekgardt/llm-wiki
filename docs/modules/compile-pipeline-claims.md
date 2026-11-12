# The compile's outcomes, derived claims, quote verification and page rendering

`scripts/llm_wiki/compile_pipeline_claims.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_claim_already_on_the_page_is_not_a_conflict.py tests/test_a_quarantined_claim_does_not_hold_its_day.py tests/test_compile_authored_citations.py tests/test_compile_bullets_keep_the_complete_physical_line.py tests/test_compile_calendar_validation_remains_exact.py tests/test_compile_evidence_uses_actual_cardinality.py tests/test_compile_literal_quote_search_preserves_unique_span.py tests/test_daily_entry_boundary.py -q`.
