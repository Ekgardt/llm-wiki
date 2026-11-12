# Tests of `scripts/generation_search.py`

- `tests/test_seal_observation_cache.py`: A generation artifact is hashed once, and any move of it is still caught.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_seal_observation_cache.py -q`.
