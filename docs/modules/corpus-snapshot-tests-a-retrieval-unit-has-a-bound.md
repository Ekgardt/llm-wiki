# Tests of `scripts/corpus_snapshot.py`

- `tests/test_a_retrieval_unit_has_a_bound.py`: A retrieval unit larger than the answer budget can only ever arrive alone.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_retrieval_unit_has_a_bound.py -q`.
