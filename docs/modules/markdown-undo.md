# Transaction undo, history pruning and deletion blockers

`scripts/llm_wiki/markdown_undo.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_new_operation_family_is_bounded_by_default.py tests/test_a_prune_keeps_what_resolves_a_quarantine.py -q`.
