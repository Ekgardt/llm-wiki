# Validated install records and the managed-resource primitives every install step shares

`scripts/install_state.py` and the tests that exercise them most.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_changed_file_is_named_and_can_be_taken_over.py tests/test_our_older_hooks_are_ours.py -q`.
