# Tests of `scripts/bounded_io.py`

- `tests/test_receipt_scoped_source_parts.py`: Fallback receipt lookup must own only a source scope and current parts.

`scripts/bounded_io.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_receipt_scoped_source_parts.py -q`.
