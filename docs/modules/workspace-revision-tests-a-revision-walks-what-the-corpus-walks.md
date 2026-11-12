# Tests of `scripts/workspace_revision.py`

- `tests/test_a_revision_walks_what_the_corpus_walks.py`: The freshness proof covers what the corpus covers, and reads git against the disk.

`scripts/workspace_revision.py` itself belongs to another module: this packet carries its exact derived
description, and these tests are the evidence that the description matches real use.

The owned files and the complete maintenance context are listed in the `-context.json`
manifest next to this guide.

Run `uv run pytest tests/test_a_revision_walks_what_the_corpus_walks.py -q`.
