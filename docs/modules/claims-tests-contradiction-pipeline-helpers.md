# Tests of `scripts/claims.py`

- `tests/contradiction_pipeline_helpers.py`: Helpers shared by test_contradiction_pipeline.py and the test files importing them.

`scripts/claims.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/contradiction_pipeline_helpers.py -q`.
