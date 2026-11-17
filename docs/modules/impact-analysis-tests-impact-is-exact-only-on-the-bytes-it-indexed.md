# Tests of `scripts/impact_analysis.py`

- `tests/test_impact_is_exact_only_on_the_bytes_it_indexed.py`: Impact is `exact` only when the generation indexed the diff's old bytes.

`scripts/impact_analysis.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_impact_is_exact_only_on_the_bytes_it_indexed.py -q`.
