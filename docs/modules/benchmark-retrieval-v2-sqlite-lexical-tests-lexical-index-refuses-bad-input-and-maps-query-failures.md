# Tests of `benchmark/retrieval_v2_sqlite_lexical.py`

- `tests/test_lexical_index_refuses_bad_input_and_maps_query_failures.py`

`benchmark/retrieval_v2_sqlite_lexical.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_lexical_index_refuses_bad_input_and_maps_query_failures.py -q`.
