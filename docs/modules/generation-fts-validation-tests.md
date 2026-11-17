# Tests of `scripts/generation_fts_validation.py`

- `tests/search_ranking_helpers.py`: Helpers shared by test_search_ranking.py and the test files importing them.

`scripts/generation_fts_validation.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/search_ranking_helpers.py -q`.
