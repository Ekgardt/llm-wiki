# Tests of `scripts/corpus_snapshot.py`

- `tests/test_whitespace_is_not_a_retrieval_chunk.py`: A paragraph cut must not publish blank evidence that its validator rejects.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_whitespace_is_not_a_retrieval_chunk.py -q`.
