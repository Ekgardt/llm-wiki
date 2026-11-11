# Tests of `scripts/fact_keys.py`

- `tests/test_a_fact_key_points_at_the_turn.py`: A user's own short facts index the turn they came from; the turn is what is read.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_fact_key_points_at_the_turn.py -q`.
