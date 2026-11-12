# Tests of `scripts/fact_keys.py`

- `tests/test_an_unanswered_turn_keeps_its_chance.py`: An omitted answer remains pending; a run stays finite and serves fresh turns first.

`scripts/fact_keys.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_unanswered_turn_keeps_its_chance.py -q`.
