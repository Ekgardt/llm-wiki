# Tests of `scripts/retrieval.py`

- `tests/test_an_abandoned_stage_is_not_a_measurement.py`: A stage that gave up on its own deadline does not teach the cost model what it cost.

`scripts/retrieval.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_abandoned_stage_is_not_a_measurement.py -q`.
