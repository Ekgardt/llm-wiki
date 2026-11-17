# Tests of `scripts/llm_client.py`

- `tests/test_the_stand_prices_its_own_ceilings.py`: Two things the stand could not price, because it did not record them.

`scripts/llm_client.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_stand_prices_its_own_ceilings.py -q`.
