# Tests of `scripts/corpus_snapshot.py`

- `tests/knowledge_extractor_helpers.py`: Helpers shared by test_knowledge_extractor.py and the test files importing them.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/knowledge_extractor_helpers.py -q`.
