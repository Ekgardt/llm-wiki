# Tests of `scripts/llm_wiki/generation_catalog_store.py`

- `tests/test_generation_catalog_store_refuses_what_no_longer_matches.py`

`scripts/llm_wiki/generation_catalog_store.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_generation_catalog_store_refuses_what_no_longer_matches.py -q`.
