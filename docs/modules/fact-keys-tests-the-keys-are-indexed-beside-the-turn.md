# Tests of `scripts/fact_keys.py`

- `tests/test_the_keys_are_indexed_beside_the_turn.py`: A turn is found under the facts it states, and the reader still gets the turn.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_keys_are_indexed_beside_the_turn.py -q`.
