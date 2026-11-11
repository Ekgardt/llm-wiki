# Tests of `scripts/generation_search.py`

- `tests/test_embedder_unavailable_names_its_reason.py`: A missing embedding model must say so, not just answer with no vectors.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_embedder_unavailable_names_its_reason.py -q`.
