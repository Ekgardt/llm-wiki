# Tests of `scripts/corpus_snapshot.py`

- `tests/test_corpus_retry_uses_the_callers_deadline.py`: A moving source may settle after four passes; no torn view is accepted.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_corpus_retry_uses_the_callers_deadline.py -q`.
