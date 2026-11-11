# Tests of `scripts/corpus_snapshot.py`

- `tests/a_decorator_is_a_call_until_it_is_a_route_helpers.py`: Helpers shared by test_a_decorator_is_a_call_until_it_is_a_route.py and the test files importing them.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/a_decorator_is_a_call_until_it_is_a_route_helpers.py -q`.
