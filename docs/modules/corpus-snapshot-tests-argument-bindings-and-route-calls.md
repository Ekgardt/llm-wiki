# Tests of `scripts/corpus_snapshot.py`

- `tests/test_argument_bindings_and_route_calls.py`: What a call passes, and which route a client call reaches (#24, B).

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_argument_bindings_and_route_calls.py -q`.
