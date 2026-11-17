# Structural + semantic lint across the vault

`scripts/lint_memory.py` and the tests that exercise them most.

Covers the `knowledge/notes/` tree (filename kept for backward compat with hooks and docs that still reference `lint_memory`).

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_short_page_still_needs_evidence_helpers.py tests/test_a_short_page_still_needs_evidence.py tests/test_an_error_is_not_an_answer.py tests/test_lint_evidence.py tests/test_lint_orphan_pages.py tests/test_quality_guards.py tests/test_source_evidence_label_counts_its_citation.py tests/test_wikilinks_tracked.py -q`.
