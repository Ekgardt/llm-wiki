# Tests of `scripts/corpus_snapshot.py`

`tests/test_corpus_snapshot.py` exercise `scripts/corpus_snapshot.py` through its public names.

`scripts/corpus_snapshot.py` itself is not part of this module: the packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_corpus_snapshot.py -q`.
