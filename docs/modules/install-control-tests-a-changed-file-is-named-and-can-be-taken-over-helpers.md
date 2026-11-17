# Tests filed with `scripts/install_control.py`

- `tests/a_changed_file_is_named_and_can_be_taken_over_helpers.py`: Helpers shared by test_a_changed_file_is_named_and_can_be_taken_over.py and the test files importing them.

These files do not import `scripts/install_control.py`, the module they are filed with. The product code they import directly is carried in this packet as follows:

- `scripts/install_state.py`: its exact derived description.
- `scripts/llm_wiki/install_v2_transactions.py`: its exact derived description.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_changed_file_is_named_and_can_be_taken_over_helpers.py -q`.
