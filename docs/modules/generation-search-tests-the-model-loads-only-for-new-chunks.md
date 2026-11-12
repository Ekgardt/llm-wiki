# Tests of `scripts/generation_search.py`

- `tests/test_the_model_loads_only_for_new_chunks.py`: A generation whose chunks are all reused does not load the embedding model.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_model_loads_only_for_new_chunks.py -q`.
