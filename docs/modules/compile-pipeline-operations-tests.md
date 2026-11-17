# Tests of `scripts/llm_wiki/compile_pipeline_operations.py`

- `tests/compile_measure_reuses_only_immutable_line_bindings_helpers.py`: Helpers shared by test_compile_measure_reuses_only_immutable_line_bindings.py and the test files importing them.
- `tests/test_a_rejected_plan_names_its_fields.py`: A compile plan refused for extra fields says which fields they were.
- `tests/test_a_slug_without_its_articles_names_the_same_page.py`: A drafted slug that differs from an existing page only by its articles updates that page.
- `tests/test_compile_does_not_update_immutable_decisions.py`: Automatic compile cannot authorize edits to an existing decision.
- `tests/test_compile_entry_overlap_is_reused_only_in_sizing_scope.py`: Repeated immutable metadata work must not rescan the source's entry table.
- `tests/test_native_compile_projection_has_complete_citation_address.py`: The displayed verified projection supplies every required citation address.
- `tests/test_source_choices_reject_unmodified_ambiguous_lines_once.py`: An unchanged ambiguous row cannot gain authority from a protected alias.

`scripts/llm_wiki/compile_pipeline_operations.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_measure_reuses_only_immutable_line_bindings_helpers.py tests/test_a_rejected_plan_names_its_fields.py tests/test_a_slug_without_its_articles_names_the_same_page.py tests/test_compile_does_not_update_immutable_decisions.py tests/test_compile_entry_overlap_is_reused_only_in_sizing_scope.py tests/test_native_compile_projection_has_complete_citation_address.py tests/test_source_choices_reject_unmodified_ambiguous_lines_once.py -q`.
