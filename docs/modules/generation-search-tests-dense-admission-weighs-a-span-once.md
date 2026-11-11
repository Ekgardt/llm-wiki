# Tests of `scripts/generation_search.py`

- `tests/test_dense_admission_weighs_a_span_once.py`: Dense admission retains its true weight without evaluating a discarded lexical one.

`scripts/generation_search.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_dense_admission_weighs_a_span_once.py -q`.
