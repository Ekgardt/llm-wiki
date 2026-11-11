# Tests of `scripts/corpus_snapshot.py`

- `tests/test_vector_reuse_keeps_the_exact_input_across_extractors.py`: An extraction namespace change must not re-encode identical proven model input.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_vector_reuse_keeps_the_exact_input_across_extractors.py -q`.
