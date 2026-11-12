# Tests of `scripts/compile_receipts.py`

- `tests/compile_binds_the_protected_source_view_helpers.py`: Helpers shared by test_compile_binds_the_protected_source_view.py and the test files importing them.
- `tests/compile_receipt_source_lifetime_helpers.py`: Helpers shared by test_compile_receipt_source_lifetime.py and the test files importing them.

`scripts/compile_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/compile_binds_the_protected_source_view_helpers.py tests/compile_receipt_source_lifetime_helpers.py -q`.
