# Tests of `scripts/fact_keys.py`

- `tests/test_a_fact_key_keeps_the_end_of_the_user_turn.py`: A fact at the end of an admitted source span must reach the extractor.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_fact_key_keeps_the_end_of_the_user_turn.py -q`.
