# Tests of `scripts/generation_search.py`

- `tests/test_the_generation_is_the_only_index.py`: The evidence generation is the only index; without one a search reads Markdown.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_generation_is_the_only_index.py -q`.
