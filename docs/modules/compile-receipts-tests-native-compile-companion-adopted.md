# Tests of `scripts/compile_receipts.py`

- `tests/test_native_compile_companion_adopted.py`: Native companion receipts remain authoritative on the adopted writer path.

`scripts/compile_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_native_compile_companion_adopted.py -q`.
