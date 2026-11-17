# Tests of `scripts/generation_search.py`

- `tests/dense_admission_trust_weight_helpers.py`: Helpers shared by test_dense_admission_trust_weight.py and the test files importing them.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/dense_admission_trust_weight_helpers.py -q`.
