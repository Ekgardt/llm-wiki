# Tests of `scripts/claims.py`

- `tests/test_native_compile_rejects_invalid_existing_ledger.py`: Publication must validate the existing ledger before adding versioned claims.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_native_compile_rejects_invalid_existing_ledger.py -q`.
