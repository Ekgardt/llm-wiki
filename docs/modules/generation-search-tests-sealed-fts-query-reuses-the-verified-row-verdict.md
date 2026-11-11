# Tests of `scripts/generation_search.py`

`tests/test_sealed_fts_query_reuses_the_verified_row_verdict.py` exercise `scripts/generation_search.py` through its public names.

`scripts/generation_search.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_sealed_fts_query_reuses_the_verified_row_verdict.py -q`.
