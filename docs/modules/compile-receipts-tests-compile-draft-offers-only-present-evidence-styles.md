# Tests of `scripts/compile_receipts.py`

- `tests/test_compile_draft_offers_only_present_evidence_styles.py`: The actual draft request cannot invite a nonexistent native source selector.

`scripts/compile_receipts.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_compile_draft_offers_only_present_evidence_styles.py -q`.
