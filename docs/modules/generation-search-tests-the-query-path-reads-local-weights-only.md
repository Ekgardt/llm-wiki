# Tests of `scripts/generation_search.py`

- `tests/test_the_query_path_reads_local_weights_only.py`: The runtime query path must not reach the network to answer a search.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_query_path_reads_local_weights_only.py -q`.
