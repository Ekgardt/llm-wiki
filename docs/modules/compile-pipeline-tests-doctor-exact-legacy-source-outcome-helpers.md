# Tests of `scripts/compile_pipeline.py`

- `tests/doctor_exact_legacy_source_outcome_helpers.py`: Helpers shared by test_doctor_exact_legacy_source_outcome.py and the test files importing them.

`scripts/compile_pipeline.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/doctor_exact_legacy_source_outcome_helpers.py -q`.
