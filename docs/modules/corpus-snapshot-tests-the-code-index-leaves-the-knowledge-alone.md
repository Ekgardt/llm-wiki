# Tests of `scripts/corpus_snapshot.py`

- `tests/test_the_code_index_leaves_the_knowledge_alone.py`: A build that names code roots collects no knowledge pages; a memory build still does.

`scripts/corpus_snapshot.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_the_code_index_leaves_the_knowledge_alone.py -q`.
