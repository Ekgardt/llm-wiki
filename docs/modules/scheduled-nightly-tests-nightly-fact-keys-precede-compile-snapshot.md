# Tests of `scripts/scheduled_nightly.py`

- `tests/test_nightly_fact_keys_precede_compile_snapshot.py`: The night's own entity writer must finish before compile freezes its targets.

`scripts/scheduled_nightly.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_nightly_fact_keys_precede_compile_snapshot.py -q`.
