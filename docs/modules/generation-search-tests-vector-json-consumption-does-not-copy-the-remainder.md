# Tests of `scripts/generation_search.py`

- `tests/test_vector_json_consumption_does_not_copy_the_remainder.py`: A flat JSON reader consumes scalar cells without copying every remaining cell.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vector_json_consumption_does_not_copy_the_remainder.py -q`.
