# Tests of `scripts/generation_fts_validation.py`

- `tests/test_foreign_code_capture_keeps_its_context.py`: A sealed code-root policy survives strict rederivation and published reads.

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_foreign_code_capture_keeps_its_context.py -q`.
