# Tests of `scripts/search_memory.py`

- `tests/test_an_answer_uses_the_published_corpus.py`: Answers use the verified captured corpus, then check selected live sources.

`scripts/search_memory.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_an_answer_uses_the_published_corpus.py -q`.
