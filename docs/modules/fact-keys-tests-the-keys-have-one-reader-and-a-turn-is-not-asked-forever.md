# Tests of `scripts/fact_keys.py`

- `tests/test_the_keys_have_one_reader_and_a_turn_is_not_asked_forever.py`: The index column alone reads keys; incomplete turns remain pending across finite runs.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_keys_have_one_reader_and_a_turn_is_not_asked_forever.py -q`.
