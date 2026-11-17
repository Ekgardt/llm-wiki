# Tests of `scripts/generation_search.py`

`tests/test_sealed_fts_query_reuses_the_verified_row_verdict.py` exercise `scripts/generation_search.py`.

`scripts/generation_search.py` itself belongs to another module: this packet carries its whole source, and these tests exercise it.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_sealed_fts_query_reuses_the_verified_row_verdict.py -q`.
