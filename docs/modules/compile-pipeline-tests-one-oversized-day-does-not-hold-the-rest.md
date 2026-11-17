# Tests of `scripts/compile_pipeline.py`

- `tests/test_one_oversized_day_does_not_hold_the_rest.py`: A day no part budget can take fails alone; the other days still compile.

`scripts/compile_pipeline.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_one_oversized_day_does_not_hold_the_rest.py -q`.
