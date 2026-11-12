# Tests of `scripts/install_control.py`

- `tests/a_changed_file_is_named_and_can_be_taken_over_helpers.py`: Helpers shared by test_a_changed_file_is_named_and_can_be_taken_over.py and the test files importing them.

`scripts/install_control.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_changed_file_is_named_and_can_be_taken_over_helpers.py -q`.
