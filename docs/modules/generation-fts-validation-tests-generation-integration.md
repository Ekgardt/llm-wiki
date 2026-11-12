# Tests of `scripts/generation_fts_validation.py`

- `tests/test_generation_integration.py`: Cross-consumer integration tests for one immutable corpus generation.

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_integration.py -q`.
